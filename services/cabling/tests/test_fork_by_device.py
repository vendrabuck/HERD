"""Unit tests for GET /internal/forks/by-device/{device_id} (issue #900).

Inventory's admin device DELETE guard calls this to find reservations whose live
fork wiring references a device (typically a transit switch that no reservation
books as a member).
"""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkStatus_ARCHIVED, ReservationFork
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

INTERNAL_TOKEN = "test-internal-token"

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    yield


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _hdr() -> dict:
    return {"X-Internal-Token": INTERNAL_TOKEN}


async def _mk_fork(rows: list[tuple], *, status: str = ForkStatus_ACTIVE) -> uuid.UUID:
    """Create a fork with (device_a_id, device_b_id) rows; returns its reservation_id."""
    reservation_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        fork = ReservationFork(reservation_id=reservation_id, canvas_data={}, status=status)
        db.add(fork)
        await db.flush()
        for i, (a, b) in enumerate(rows):
            db.add(
                ForkConnection(
                    fork_id=fork.id,
                    device_a_id=a,
                    port_a=f"a{i}",
                    device_b_id=b,
                    port_b=f"b{i}",
                    layer="L1",
                    created_by="system",
                )
            )
        await db.commit()
    return reservation_id


async def _by_device(client, device_id, *, headers: dict | None = None):
    return await client.get(
        f"/internal/forks/by-device/{device_id}",
        headers=_hdr() if headers is None else headers,
    )


@pytest.mark.asyncio
async def test_wrong_token_is_403(client):
    resp = await _by_device(client, uuid.uuid4(), headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Invalid internal token"}


@pytest.mark.asyncio
async def test_missing_token_is_422(client):
    resp = await client.get(f"/internal/forks/by-device/{uuid.uuid4()}")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_no_forks_is_empty(client):
    resp = await _by_device(client, uuid.uuid4())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reservation_ids": []}


@pytest.mark.asyncio
async def test_unknown_device_with_other_forks_present_is_empty(client):
    await _mk_fork([(uuid.uuid4(), uuid.uuid4())])
    resp = await _by_device(client, uuid.uuid4())
    assert resp.status_code == 200
    assert resp.json() == {"reservation_ids": []}


@pytest.mark.asyncio
async def test_device_as_source(client):
    dev = uuid.uuid4()
    rid = await _mk_fork([(dev, uuid.uuid4())])
    resp = await _by_device(client, dev)
    assert resp.json() == {"reservation_ids": [str(rid)]}


@pytest.mark.asyncio
async def test_device_as_target(client):
    dev = uuid.uuid4()
    rid = await _mk_fork([(uuid.uuid4(), dev)])
    resp = await _by_device(client, dev)
    assert resp.json() == {"reservation_ids": [str(rid)]}


@pytest.mark.asyncio
async def test_device_as_middle_hop_only(client):
    a, s, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rid = await _mk_fork([(a, s), (s, b)])
    resp = await _by_device(client, s)
    assert resp.json() == {"reservation_ids": [str(rid)]}


@pytest.mark.asyncio
async def test_many_hops_in_one_fork_yield_one_id(client):
    s = uuid.uuid4()
    rid = await _mk_fork([(uuid.uuid4(), s), (s, uuid.uuid4()), (s, uuid.uuid4())])
    resp = await _by_device(client, s)
    assert resp.json() == {"reservation_ids": [str(rid)]}


@pytest.mark.asyncio
async def test_archived_fork_does_not_count(client):
    s = uuid.uuid4()
    await _mk_fork([(uuid.uuid4(), s), (s, uuid.uuid4())], status=ForkStatus_ARCHIVED)
    resp = await _by_device(client, s)
    assert resp.json() == {"reservation_ids": []}


@pytest.mark.asyncio
async def test_two_active_forks_sorted_and_archived_excluded(client):
    s = uuid.uuid4()
    r1 = await _mk_fork([(uuid.uuid4(), s)])
    r2 = await _mk_fork([(s, uuid.uuid4())])
    await _mk_fork([(s, uuid.uuid4())], status=ForkStatus_ARCHIVED)
    resp = await _by_device(client, s)
    assert resp.status_code == 200
    assert resp.json() == {"reservation_ids": sorted([str(r1), str(r2)])}
