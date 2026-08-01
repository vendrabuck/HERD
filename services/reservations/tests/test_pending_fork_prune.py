"""Pending fork-prune marker and sweep reconciler (issues #459 and #462).

The device-set PATCH's REMOVE half must converge without human action: the PATCH
writes the removed device ids into reservations.pending_fork_prune_device_ids in the
SAME transaction as the committed edit, the best-effort prune clears exactly the ids
it converged (set-difference under a row lock, so a concurrent PATCH's union
survives), and the expiration sweep's pending-prune reconciler retries whatever is
left each tick, dropping the marker once the reservation goes terminal (ledger
teardown owns that release).

Covers, at the reservations boundary:

- the PATCH writes (and unions into) the marker atomically with the edit;
- wrapper outcomes: success clears, failure keeps, ARCHIVED converges, a 404 keeps
  the marker only while a fork can still appear (ACTIVE with a parent topology);
- the sweep reconciler: retry-and-clear on success, keep-and-retry on repeated
  failure, terminal-meanwhile clears without a cabling call.

stage_wiring_changed, the wrapper, and the sweep open their own AsyncSessionLocal
against the app engine, so this suite shares that engine (mirrors
test_wiring_changed_staging).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings
from app.database import Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from app.services.reservation_service import (
    _clear_pending_fork_prune,
    _prune_removed_devices_from_fork_best_effort,
)
from app.tasks.expiration import _run_pending_prune_reconcile
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)
INTERNAL_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    # The best-effort wrapper early-returns without an internal token; these tests
    # exercise the full path.
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    yield


async def _insert(
    status: ReservationStatus,
    *,
    device_ids: list[str] | None = None,
    topology_id: uuid.UUID | None = None,
    pending: list[str] | None = None,
) -> uuid.UUID:
    res = Reservation(
        user_id=USER_ID,
        owner_name="owner",
        device_ids=device_ids or [str(uuid.uuid4())],
        topology_id=topology_id,
        topology_type=TopologyType.PHYSICAL,
        purpose="t",
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=2),
        status=status,
        pending_fork_prune_device_ids=pending,
    )
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _marker(reservation_id: uuid.UUID) -> list | None:
    async with TestSessionLocal() as db:
        row = (
            await db.execute(
                select(Reservation.pending_fork_prune_device_ids).where(
                    Reservation.id == reservation_id
                )
            )
        ).first()
        return row[0] if row is not None else None


def _prune_resp(status_code: int, body: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    resp.json.return_value = body or {}
    return resp


def _changed_false_body() -> dict:
    return {
        "fork_id": str(uuid.uuid4()),
        "version_number": 1,
        "changed": False,
        "released": [],
    }


def _client_as(sub: str, role: str = "user") -> AsyncClient:
    app.dependency_overrides[get_db] = get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="fake-token"
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _device(device_id: str) -> dict:
    return {
        "id": device_id,
        "name": f"dev-{device_id[:8]}",
        "status": "AVAILABLE",
        "topology_type": "PHYSICAL",
        "exclusive": True,
    }


# --- The PATCH writes the marker atomically with the edit ----------------------------


@pytest.mark.asyncio
async def test_patch_remove_writes_marker_with_the_edit():
    """PATCH-removing a device from an ACTIVE reservation records the removed id in
    pending_fork_prune_device_ids; the write commits with the edit, independent of
    whether the best-effort prune afterwards succeeds (here it is inert)."""
    keep, remove = str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, remove])

    async def fetch_devices(ids, token=""):
        return [_device(str(d)) for d in ids]

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=fetch_devices),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
        patch(
            "app.services.reservation_service._prune_removed_devices_from_fork_best_effort",
            new=AsyncMock(),
        ) as prune,
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.patch(f"/{rid}", json={"device_ids": [keep]})

    assert resp.status_code == 200
    assert await _marker(rid) == [remove]
    prune.assert_awaited_once()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_patch_remove_unions_into_existing_marker():
    """A second removal while an earlier prune is still pending unions its ids into
    the marker rather than clobbering them."""
    keep, remove = str(uuid.uuid4()), str(uuid.uuid4())
    stale = str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, remove], pending=[stale])

    async def fetch_devices(ids, token=""):
        return [_device(str(d)) for d in ids]

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=fetch_devices),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
        patch(
            "app.services.reservation_service._prune_removed_devices_from_fork_best_effort",
            new=AsyncMock(),
        ),
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.patch(f"/{rid}", json={"device_ids": [keep]})

    assert resp.status_code == 200
    assert await _marker(rid) == sorted({stale, remove})
    app.dependency_overrides.clear()


# --- _clear_pending_fork_prune: set-difference, never a blind null-out ---------------


@pytest.mark.asyncio
async def test_clear_removes_only_the_pruned_ids():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, pending=[a, b])

    await _clear_pending_fork_prune(rid, [a])
    assert await _marker(rid) == [b]

    await _clear_pending_fork_prune(rid, [b])
    assert await _marker(rid) is None


# --- The best-effort wrapper's outcome-to-marker mapping -----------------------------


@pytest.mark.asyncio
async def test_wrapper_success_clears_marker():
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_prune_resp(200, _changed_false_body())),
    ):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)

    assert await _marker(rid) is None


@pytest.mark.asyncio
async def test_wrapper_failure_keeps_marker():
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=RuntimeError("cabling down")),
    ):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)

    assert await _marker(rid) == [str(dut)]


@pytest.mark.asyncio
async def test_wrapper_partial_clear_keeps_concurrently_added_ids():
    """Clearing after a converged prune removes only the ids that prune covered: an
    id a concurrent PATCH unioned in meanwhile survives for the sweep."""
    pruned, concurrent = uuid.uuid4(), uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(pruned), str(concurrent)])

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_prune_resp(200, _changed_false_body())),
    ):
        await _prune_removed_devices_from_fork_best_effort(rid, [pruned], attempts=1)

    assert await _marker(rid) == [str(concurrent)]


@pytest.mark.asyncio
async def test_wrapper_archived_409_clears_marker():
    """An ARCHIVED fork converges (terminal teardown owns the release): clear."""
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(
            return_value=_prune_resp(409, {"detail": "Fork is archived and cannot be edited"})
        ),
    ):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)

    assert await _marker(rid) is None


@pytest.mark.asyncio
async def test_wrapper_no_fork_keeps_marker_while_fork_may_appear():
    """A 404 on an ACTIVE topology-carrying reservation is temporary: the sweep's
    missing-fork backstop builds the fork FROM THE PARENT CANVAS, removed devices
    included, so the pending prune must survive to scrub them once it exists."""
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=uuid.uuid4(), pending=[str(dut)])

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_prune_resp(404)),
    ):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)

    assert await _marker(rid) == [str(dut)]


@pytest.mark.asyncio
async def test_wrapper_no_fork_clears_marker_without_topology():
    """Without a parent topology only an empty lazy-created fork can ever appear
    (Case A), which holds nothing for a removed device: the 404 is final."""
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=None, pending=[str(dut)])

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_prune_resp(404)),
    ):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)

    assert await _marker(rid) is None


# --- The sweep reconciler (#462): durable convergence without human action -----------


@pytest.mark.asyncio
async def test_sweep_retries_pending_prune_and_clears_on_success():
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])
    calls = []

    async def fork_call(method, path, json_body=None):
        calls.append((method, path, json_body))
        return _prune_resp(200, _changed_false_body())

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=fork_call),
    ):
        await _run_pending_prune_reconcile()

    assert calls == [("POST", f"/internal/forks/{rid}/prune-devices", {"device_ids": [str(dut)]})]
    assert await _marker(rid) is None


@pytest.mark.asyncio
async def test_sweep_repeated_failure_keeps_marker_and_retries_each_tick():
    """A persistent cabling outage retries once per tick, forever, never dropping the
    marker: giving up would recreate issue #462's stranded wiring."""
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])
    mock_call = AsyncMock(side_effect=RuntimeError("cabling down"))

    with patch("app.services.reservation_service._cabling_fork_call", new=mock_call):
        await _run_pending_prune_reconcile()
        await _run_pending_prune_reconcile()

    assert mock_call.await_count == 2, "one attempt per tick (the PATCH already backed off)"
    assert await _marker(rid) == [str(dut)]


@pytest.mark.asyncio
async def test_sweep_terminal_reservation_clears_marker_without_prune():
    """A reservation that went terminal meanwhile drops its marker with NO cabling
    call: terminal teardown releases from execution's ledgers and the fork archive
    settles the claims, so there is nothing left for a prune to do."""
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.COMPLETED, pending=[str(dut)])
    mock_call = AsyncMock()

    with patch("app.services.reservation_service._cabling_fork_call", new=mock_call):
        await _run_pending_prune_reconcile()

    mock_call.assert_not_awaited()
    assert await _marker(rid) is None
