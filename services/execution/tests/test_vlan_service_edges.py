"""Edge-branch coverage for vlan_service.py.

Covers fetch_fabric_id (network helper, all outcomes, fail closed per issue
#1003), the FabricResolver memo, the no-free-VLAN
exhaustion in find_or_assign_vlan, and the retry-exhaustion guard. The happy
paths are covered in test_vlan_service.py, and so is the IntegrityError-retry
race: test_assign_vlan_loses_race_retries_onto_free_vlan and
test_assign_vlan_race_with_own_redelivery_retry_finds_own_row commit a competitor
row between the caller's read and its commit.
"""

import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from app.database import Base
from app.models.vlan_assignment import VlanAssignment
from app.services import vlan_service
from app.services.nats_consumer import PermanentEventError, TransientUpstreamError
from app.services.vlan_service import (
    VLAN_MAX,
    VLAN_MIN,
    FabricResolver,
    _derive_vlan_id,
    fetch_fabric_id,
    find_or_assign_vlan,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

FABRIC = uuid.uuid5(uuid.NAMESPACE_DNS, "vlan-edge-fabric")
SWITCH = str(uuid.uuid4())


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


# --- fetch_fabric_id ---


class _FakeClient:
    def __init__(self, resp=None, exc: Exception | None = None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _resp(status_code: int, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {}
    return r


@pytest.mark.asyncio
async def test_fetch_fabric_id_success(monkeypatch):
    fabric_id = uuid.uuid4()
    monkeypatch.setattr(
        vlan_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(200, {"fabric_id": str(fabric_id)})),
    )
    result = await fetch_fabric_id(str(uuid.uuid4()))
    assert result == fabric_id


@pytest.mark.parametrize("status", [403, 404, 500, 503])
@pytest.mark.asyncio
async def test_fetch_fabric_id_non_200_raises_transient(monkeypatch, status):
    """Any non-200 answer fails closed (issue #1003): no None, no stand-in fabric."""
    monkeypatch.setattr(
        vlan_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(status)),
    )
    device = str(uuid.uuid4())
    with pytest.raises(TransientUpstreamError) as info:
        await fetch_fabric_id(device)
    assert str(info.value) == f"cabling fabric lookup for device {device}: upstream {status}"


@pytest.mark.asyncio
async def test_fetch_fabric_id_transport_error_raises_transient(monkeypatch):
    """A transport error fails closed and names only the exception class."""
    monkeypatch.setattr(
        vlan_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(exc=httpx.ConnectError("down at 10.0.0.9")),
    )
    device = str(uuid.uuid4())
    with pytest.raises(TransientUpstreamError) as info:
        await fetch_fabric_id(device)
    assert str(info.value) == (
        f"cabling fabric lookup for device {device}: transport error ConnectError"
    )


@pytest.mark.parametrize("payload", [{}, {"fabric_id": "not-a-uuid"}, {"fabric_id": None}])
@pytest.mark.asyncio
async def test_fetch_fabric_id_unparseable_answer_raises_transient(monkeypatch, payload):
    monkeypatch.setattr(
        vlan_service.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(200, payload)),
    )
    device = str(uuid.uuid4())
    with pytest.raises(TransientUpstreamError, match="unparseable answer"):
        await fetch_fabric_id(device)


@pytest.mark.asyncio
async def test_fabric_resolver_memoizes_and_does_not_cache_failures():
    calls: list[str] = []
    fabric = uuid.uuid4()
    fail_once = {"left": 1}

    async def fetch(switch_id):
        calls.append(switch_id)
        if fail_once["left"]:
            fail_once["left"] -= 1
            raise TransientUpstreamError("down")
        return fabric

    resolver = FabricResolver(fetch=fetch)
    with pytest.raises(TransientUpstreamError):
        await resolver.fabric_of(SWITCH)
    assert await resolver.fabric_of(SWITCH) == fabric
    assert await resolver.fabric_of(uuid.UUID(SWITCH)) == fabric
    assert calls == [SWITCH, SWITCH]


def _resolver():
    async def fetch(switch_id):
        return FABRIC

    return FabricResolver(fetch=fetch)


# --- find_or_assign_vlan: no free VLAN (line 112) ---


@pytest.mark.asyncio
async def test_assign_vlan_raises_when_all_in_use(db, monkeypatch):
    """When every VLAN in range is taken, the assignment raises PermanentEventError.

    We shrink the VLAN range to a single value so the in-use set can saturate
    it cheaply: pre-assign that one VLAN to another reservation, then the caller
    (whose preferred VLAN collides) finds no free candidate and raises. The error
    is a PermanentEventError (not a bare RuntimeError) so the NATS consumer DLQs
    it on first delivery instead of retrying a condition retry cannot fix (#211).
    """
    # Collapse the usable range to exactly one VLAN id (VLAN_MIN..VLAN_MIN).
    monkeypatch.setattr(vlan_service, "VLAN_MAX", VLAN_MIN)

    occupant = str(uuid.uuid4())
    db.add(
        VlanAssignment(
            reservation_id=uuid.UUID(occupant),
            fabric_id=FABRIC,
            vlan_id=VLAN_MIN,
            switch_device_ids=[SWITCH],
            status="ACTIVE",
        )
    )
    await db.commit()

    # The caller's preferred id, derived against the shrunk range, is VLAN_MIN
    # (the only value), which is already in use, so the lowest-free scan fails.
    rid = str(uuid.uuid4())
    with pytest.raises(PermanentEventError, match="No free VLAN"):
        await find_or_assign_vlan(db, rid, FABRIC, [SWITCH], _resolver())


# --- find_or_assign_vlan: retry exhaustion (line 143) ---


@pytest.mark.asyncio
async def test_assign_vlan_raises_on_persistent_contention(db, monkeypatch):
    """If every commit attempt trips IntegrityError, the retry cap raises RuntimeError.

    Stub db.commit to always raise IntegrityError so the loop exhausts
    _MAX_ASSIGN_RETRIES without ever committing, hitting the final guard.
    """
    from sqlalchemy.exc import IntegrityError

    async def _always_conflict():
        raise IntegrityError("stmt", {}, Exception("dup"))

    rollbacks = {"n": 0}
    real_rollback = db.rollback

    async def _count_rollback():
        rollbacks["n"] += 1
        await real_rollback()

    monkeypatch.setattr(db, "commit", _always_conflict)
    monkeypatch.setattr(db, "rollback", _count_rollback)

    rid = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="persistent contention"):
        await find_or_assign_vlan(db, rid, FABRIC, [SWITCH], _resolver())

    # One rollback per retry attempt.
    assert rollbacks["n"] == vlan_service._MAX_ASSIGN_RETRIES


def test_vlan_range_constants_sane():
    assert VLAN_MIN < VLAN_MAX
    rid = str(uuid.uuid4())
    assert VLAN_MIN <= _derive_vlan_id(rid) <= VLAN_MAX
