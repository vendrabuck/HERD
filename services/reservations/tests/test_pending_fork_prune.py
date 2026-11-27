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
  failure, terminal-meanwhile clears without a cabling call, entries that do not parse
  as a UUID are cleared while the valid entries beside them are kept and retried;
- the stored-state invariant (issue #1522): the column holds SQL NULL exactly when
  nothing is pending, whatever path emptied the marker or never set it, so the
  reconciler's real statement never selects such a row; with more than a batch of
  emptied rows ahead of it a new marker is still pruned; the batch is the oldest
  20 markers, oldest first. Stored state is read at the SQL level (`IS NULL` and the
  raw JSON text), never by comparing the loaded value with None: a JSON "null" also
  loads as None, which is how the earlier assertions here missed the defect;
- the reconciler's heal: a selected marker holding nothing actionable (JSON "null",
  an empty list, or a corrupt non-list value, seeded as raw stored text) is SQL NULL
  after one tick in any status, a corrupt one never stops the rows after it, and a
  real list written between the SELECT and the heal survives.

stage_wiring_changed, the wrapper, and the sweep open their own AsyncSessionLocal
against the app engine, so this suite shares that engine (mirrors
test_wiring_changed_staging).
"""

import json
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
    _heal_non_actionable_prune_marker,
    _prune_removed_devices_from_fork_best_effort,
)
from app.tasks.expiration import (
    _PENDING_PRUNE_BATCH,
    _pending_prune_stmt,
    _run_pending_prune_reconcile,
)
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Text, literal, select, type_coerce, update
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
INTERNAL_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def _clock():
    """Read the clock when each test starts, never at import (#1561)."""
    global NOW
    NOW = datetime.now(timezone.utc)


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


_OMIT = object()


async def _insert(
    status: ReservationStatus,
    *,
    device_ids: list[str] | None = None,
    topology_id: uuid.UUID | None = None,
    pending=None,
) -> uuid.UUID:
    """Insert one reservation. `pending=None` passes an explicit None to the
    constructor; `pending=_OMIT` leaves the attribute unset, as create_reservation
    does, so the INSERT omits the column."""
    marker = {} if pending is _OMIT else {"pending_fork_prune_device_ids": pending}
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
        **marker,
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


# --- Stored-state invariant (issue #1522) ---------------------------------------------
#
# The reconciler's only "something is pending" test is `IS NOT NULL`, so the column
# must hold SQL NULL exactly when nothing is pending. Every helper below reads the
# database's own answer: `_stored` returns SQL_NULL when `IS NULL` is true there and the
# raw JSON text otherwise, and `_selected` runs the reconciler's real statement.

SQL_NULL = "SQL NULL"


async def _stored(reservation_id: uuid.UUID) -> str:
    col = Reservation.pending_fork_prune_device_ids
    async with TestSessionLocal() as db:
        is_null, raw = (
            await db.execute(
                select(col.is_(None), type_coerce(col, Text)).where(
                    Reservation.id == reservation_id
                )
            )
        ).one()
    return SQL_NULL if is_null else raw


async def _selected() -> list[uuid.UUID]:
    async with TestSessionLocal() as db:
        return [row.id for row in (await db.execute(_pending_prune_stmt())).all()]


async def _set_updated_at(reservation_id: uuid.UUID, when: datetime) -> None:
    async with TestSessionLocal() as db:
        await db.execute(
            update(Reservation).where(Reservation.id == reservation_id).values(updated_at=when)
        )
        await db.commit()


def _cabling_answers(status_code: int, body: dict | None = None):
    return patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_prune_resp(status_code, body)),
    )


async def _fetch_devices(ids, token=""):
    return [_device(str(d)) for d in ids]


# Every way a row ends up with nothing pending. Each returns the reservation id.


async def _never_set_column_omitted() -> uuid.UUID:
    # create_reservation never names the column, so its INSERT omits it.
    return await _insert(ReservationStatus.ACTIVE, pending=_OMIT)


async def _never_set_explicit_none() -> uuid.UUID:
    return await _insert(ReservationStatus.ACTIVE, pending=None)


async def _cleared_in_one_call() -> uuid.UUID:
    a = str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, pending=[a])
    await _clear_pending_fork_prune(rid, [a])
    return rid


async def _cleared_in_two_calls() -> uuid.UUID:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, pending=[a, b])
    await _clear_pending_fork_prune(rid, [a])
    assert json.loads(await _stored(rid)) == [b], "a partial clear keeps the rest"
    await _clear_pending_fork_prune(rid, [b])
    return rid


async def _fast_path_converged() -> uuid.UUID:
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])
    with _cabling_answers(200, _changed_false_body()):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)
    return rid


async def _fast_path_archived_409() -> uuid.UUID:
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])
    with _cabling_answers(409, {"detail": "Fork is archived and cannot be edited"}):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)
    return rid


async def _fast_path_final_404() -> uuid.UUID:
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=None, pending=[str(dut)])
    with _cabling_answers(404):
        await _prune_removed_devices_from_fork_best_effort(rid, [dut], attempts=1)
    return rid


async def _sweep_retry_converged() -> uuid.UUID:
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(uuid.uuid4())])
    with _cabling_answers(200, _changed_false_body()):
        await _run_pending_prune_reconcile()
    return rid


async def _sweep_terminal_clear() -> uuid.UUID:
    rid = await _insert(ReservationStatus.COMPLETED, pending=[str(uuid.uuid4())])
    with _cabling_answers(200, _changed_false_body()) as call:
        await _run_pending_prune_reconcile()
    call.assert_not_awaited()
    return rid


async def _sweep_corrupt_entries_clear() -> uuid.UUID:
    rid = await _insert(ReservationStatus.ACTIVE, pending=["not-a-uuid"])
    with _cabling_answers(200, _changed_false_body()) as call:
        await _run_pending_prune_reconcile()
    call.assert_not_awaited()
    return rid


async def _patch_remove_then_fast_path() -> uuid.UUID:
    keep, remove = str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, remove], pending=_OMIT)
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=_fetch_devices),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        _cabling_answers(200, _changed_false_body()) as call,
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.patch(f"/{rid}", json={"device_ids": [keep]})
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    call.assert_awaited_once_with(
        "POST", f"/internal/forks/{rid}/prune-devices", json_body={"device_ids": [remove]}
    )
    return rid


_EMPTYING_PATHS = {
    "never_set_column_omitted": _never_set_column_omitted,
    "never_set_explicit_none": _never_set_explicit_none,
    "cleared_in_one_call": _cleared_in_one_call,
    "cleared_in_two_calls": _cleared_in_two_calls,
    "fast_path_converged": _fast_path_converged,
    "fast_path_archived_409": _fast_path_archived_409,
    "fast_path_final_404": _fast_path_final_404,
    "sweep_retry_converged": _sweep_retry_converged,
    "sweep_terminal_clear": _sweep_terminal_clear,
    "sweep_corrupt_entries_clear": _sweep_corrupt_entries_clear,
    "patch_remove_then_fast_path": _patch_remove_then_fast_path,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(_EMPTYING_PATHS))
async def test_every_emptying_path_stores_sql_null_and_is_never_selected(path):
    rid = await _EMPTYING_PATHS[path]()

    assert await _stored(rid) == SQL_NULL
    assert rid not in await _selected()


@pytest.mark.asyncio
async def test_union_during_an_inflight_prune_keeps_a_list_then_clears_to_sql_null():
    """A device-set PATCH that unions a new id into the marker while the sweep's prune
    of an older id is in flight: the sweep's clear leaves exactly the new id (a
    non-empty list, still selected), and the next tick's clear stores SQL NULL."""
    keep, older, newer = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, newer], pending=[older])
    pruned: list[list[str]] = []
    patch_codes: list[int] = []

    async def fork_call(method, path, json_body=None):
        pruned.append(json_body["device_ids"])
        if json_body["device_ids"] == [older]:
            async with _client_as(str(USER_ID)) as ac:
                resp = await ac.patch(f"/{rid}", json={"device_ids": [keep]})
            patch_codes.append(resp.status_code)
        return _prune_resp(200, _changed_false_body())

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=_fetch_devices),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        patch(
            "app.services.reservation_service._cabling_fork_call",
            new=AsyncMock(side_effect=fork_call),
        ),
        # The PATCH's own fast-path prune of `newer` is held back, as if it had
        # failed, so the sweep is what converges it.
        patch(
            "app.services.reservation_service._prune_removed_devices_from_fork_best_effort",
            new=AsyncMock(),
        ) as patch_fast_path,
    ):
        await _run_pending_prune_reconcile()
        app.dependency_overrides.clear()

        assert patch_codes == [200]
        patch_fast_path.assert_awaited_once()
        assert json.loads(await _stored(rid)) == [newer]
        assert await _selected() == [rid]

        await _run_pending_prune_reconcile()

    assert pruned == [[older], [newer]]
    assert await _stored(rid) == SQL_NULL
    assert await _selected() == []


@pytest.mark.asyncio
async def test_a_new_marker_is_pruned_behind_more_than_a_batch_of_emptied_rows():
    """The starvation of issue #1522: more than _PENDING_PRUNE_BATCH rows emptied by
    every path, all older than one new marker. The reconciler's batch is oldest
    first, so it reaches the new marker only if no emptied row is selected."""
    builders = [_EMPTYING_PATHS[name] for name in sorted(_EMPTYING_PATHS)]
    emptied = [await builders[i % len(builders)]() for i in range(_PENDING_PRUNE_BATCH + 5)]
    oldest = NOW - timedelta(days=1)
    for i, rid in enumerate(emptied):
        await _set_updated_at(rid, oldest + timedelta(seconds=i))
    dut = uuid.uuid4()
    fresh = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])
    await _set_updated_at(fresh, NOW + timedelta(days=1))
    calls = []

    async def fork_call(method, path, json_body=None):
        calls.append((method, path, json_body))
        return _prune_resp(200, _changed_false_body())

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=fork_call),
    ):
        await _run_pending_prune_reconcile()

    assert calls == [("POST", f"/internal/forks/{fresh}/prune-devices", {"device_ids": [str(dut)]})]
    assert await _stored(fresh) == SQL_NULL


@pytest.mark.asyncio
async def test_each_tick_retries_the_oldest_batch_oldest_first_and_the_rest_next_tick():
    """RES-FORK-17: a tick retries at most _PENDING_PRUNE_BATCH (20) markers, ordered
    by updated_at ascending; the backlog past the cap waits for the next tick. The
    rows are inserted newest first, so insertion order is the reverse of age."""
    assert _PENDING_PRUNE_BATCH == 20
    count = _PENDING_PRUNE_BATCH + 5
    oldest = NOW - timedelta(days=1)
    by_age: dict[int, uuid.UUID] = {}
    for i in reversed(range(count)):
        rid = await _insert(ReservationStatus.ACTIVE, pending=[str(uuid.uuid4())])
        await _set_updated_at(rid, oldest + timedelta(seconds=i))
        by_age[i] = rid
    oldest_first = [by_age[i] for i in range(count)]
    ticks: list[list[uuid.UUID]] = []

    async def fork_call(method, path, json_body=None):
        ticks[-1].append(uuid.UUID(path.split("/")[3]))
        return _prune_resp(200, _changed_false_body())

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=fork_call),
    ):
        for _ in range(2):
            ticks.append([])
            await _run_pending_prune_reconcile()

    assert ticks == [oldest_first[:_PENDING_PRUNE_BATCH], oldest_first[_PENDING_PRUNE_BATCH:]]
    assert await _selected() == []


@pytest.mark.asyncio
async def test_non_uuid_entries_are_cleared_and_every_valid_entry_is_kept_and_pruned():
    """RES-FORK-17 (issue #1531): an ACTIVE row's marker entries that do not parse as a
    UUID are cleared on the tick that meets them, with no prune, while the valid entries
    on the same row and on another row are pruned and kept until a prune converges. The
    entries here are a non-UUID string, a number, and a JSON null. The first
    tick's cabling is down, so what remains after it is exactly what the bad-entry
    clear left; the second tick converges and both rows leave the selection."""
    v1, v2, v3 = (str(uuid.uuid4()) for _ in range(3))
    mixed = await _insert(ReservationStatus.ACTIVE, pending=["not-a-uuid", v1, 7, None, v2])
    valid_only = await _insert(ReservationStatus.ACTIVE, pending=[v3])
    await _set_updated_at(mixed, NOW - timedelta(minutes=2))
    await _set_updated_at(valid_only, NOW - timedelta(minutes=1))
    expected_calls = [
        ("POST", f"/internal/forks/{mixed}/prune-devices", {"device_ids": [v1, v2]}),
        ("POST", f"/internal/forks/{valid_only}/prune-devices", {"device_ids": [v3]}),
    ]
    calls: list = []

    async def cabling_down(method, path, json_body=None):
        calls.append((method, path, json_body))
        raise RuntimeError("cabling down")

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=cabling_down),
    ):
        await _run_pending_prune_reconcile()

    async def entries(rid):
        stored = await _stored(rid)
        return stored if stored == SQL_NULL else json.loads(stored)

    assert calls == expected_calls
    assert await entries(mixed) == [v1, v2]
    assert await entries(valid_only) == [v3]
    assert set(await _selected()) == {mixed, valid_only}

    calls.clear()
    with _recording_cabling(calls):
        await _run_pending_prune_reconcile()

    assert sorted(calls) == sorted(expected_calls)
    assert await _stored(mixed) == SQL_NULL
    assert await _stored(valid_only) == SQL_NULL
    assert await _selected() == []


# --- The reconciler heals a selected marker with nothing actionable (#1522) -----------
#
# Current writers never store these forms, but a process still on the previous code (or
# a rollback, or a hand edit) can, and the reconciler selects every non-NULL marker. The
# shapes are written as raw stored text, bypassing the model's JSON serializer.

_NON_ACTIONABLE = {
    "json_null": "null",
    "empty_list": "[]",
    "empty_string": '""',
    "zero": "0",
    "false": "false",
    "empty_object": "{}",
    "true": "true",
    "five": "5",
}


async def _store_raw(reservation_id: uuid.UUID, raw: str) -> None:
    async with TestSessionLocal() as db:
        await db.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id)
            .values(pending_fork_prune_device_ids=literal(raw, Text))
        )
        await db.commit()


def _recording_cabling(calls: list):
    async def fork_call(method, path, json_body=None):
        calls.append((method, path, json_body))
        return _prune_resp(200, _changed_false_body())

    return patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=fork_call),
    )


async def _seed_non_actionable(raw: str, status: ReservationStatus) -> uuid.UUID:
    rid = await _insert(status, pending=[str(uuid.uuid4())])
    await _store_raw(rid, raw)
    await _set_updated_at(rid, NOW - timedelta(days=1))
    assert await _stored(rid) != SQL_NULL
    assert rid in await _selected()
    return rid


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ReservationStatus.ACTIVE, ReservationStatus.COMPLETED])
@pytest.mark.parametrize("shape", sorted(_NON_ACTIONABLE))
async def test_a_non_actionable_marker_is_healed_and_the_rows_behind_it_still_run(shape, status):
    """Seeded OLDEST, so it is first in the batch: one tick stores SQL NULL for it (it
    leaves the selection) and still prunes the real marker behind it."""
    bad = await _seed_non_actionable(_NON_ACTIONABLE[shape], status)
    dut = uuid.uuid4()
    real = await _insert(ReservationStatus.ACTIVE, pending=[str(dut)])
    await _set_updated_at(real, NOW)
    calls: list = []

    with _recording_cabling(calls):
        await _run_pending_prune_reconcile()

    assert await _stored(bad) == SQL_NULL
    assert calls == [("POST", f"/internal/forks/{real}/prune-devices", {"device_ids": [str(dut)]})]
    assert await _stored(real) == SQL_NULL
    assert await _selected() == []


@pytest.mark.asyncio
async def test_a_corrupt_marker_is_logged_at_error_by_type_only(caplog):
    caplog.set_level("WARNING", logger="app.tasks.expiration")
    bad = await _insert(ReservationStatus.ACTIVE, pending=[str(uuid.uuid4())])
    await _store_raw(bad, '{"leak": "marker-text"}')
    empty = await _insert(ReservationStatus.ACTIVE, pending=[str(uuid.uuid4())])
    await _store_raw(empty, "null")

    with _recording_cabling([]):
        await _run_pending_prune_reconcile()
        await _run_pending_prune_reconcile()

    corrupt = [
        r
        for r in caplog.records
        if getattr(r, "action", "") == "pending_prune_corrupt_marker_healed"
    ]
    healed_empty = [
        r for r in caplog.records if getattr(r, "action", "") == "pending_prune_empty_marker_healed"
    ]
    assert [(r.levelname, r.reservation_id, r.marker_type) for r in corrupt] == [
        ("ERROR", str(bad), "dict")
    ]
    assert [(r.levelname, r.reservation_id) for r in healed_empty] == [("WARNING", str(empty))]
    for record in corrupt + healed_empty:
        assert "marker-text" not in record.getMessage()
        assert "marker-text" not in repr(vars(record))
    assert await _stored(bad) == SQL_NULL
    assert await _stored(empty) == SQL_NULL


@pytest.mark.asyncio
async def test_a_real_marker_written_between_the_select_and_the_heal_survives():
    """A device-set PATCH may union real ids into the marker after the reconciler's
    SELECT read it as JSON "null": the heal reads it again under the row lock and
    leaves it, and the next tick prunes it."""
    rid = await _seed_non_actionable("null", ReservationStatus.ACTIVE)
    unioned = str(uuid.uuid4())

    async def write_then_heal(reservation_id):
        async with TestSessionLocal() as db:
            await db.execute(
                update(Reservation)
                .where(Reservation.id == reservation_id)
                .values(pending_fork_prune_device_ids=[unioned])
            )
            await db.commit()
        return await _heal_non_actionable_prune_marker(reservation_id)

    calls: list = []
    with (
        _recording_cabling(calls),
        patch(
            "app.tasks.expiration._heal_non_actionable_prune_marker",
            new=AsyncMock(side_effect=write_then_heal),
        ) as heal,
    ):
        await _run_pending_prune_reconcile()

    heal.assert_awaited_once_with(rid)
    assert calls == []
    assert json.loads(await _stored(rid)) == [unioned]
    assert await _selected() == [rid]

    with _recording_cabling(calls):
        await _run_pending_prune_reconcile()

    assert calls == [("POST", f"/internal/forks/{rid}/prune-devices", {"device_ids": [unioned]})]
    assert await _stored(rid) == SQL_NULL


@pytest.mark.asyncio
async def test_the_heal_leaves_sql_null_and_real_markers_unwritten():
    """The heal writes only a stored non-actionable value: a row already SQL NULL or
    holding a real list is not updated (its updated_at does not move)."""
    stamp = NOW - timedelta(days=3)
    already_null = await _insert(ReservationStatus.ACTIVE, pending=_OMIT)
    real = await _insert(ReservationStatus.ACTIVE, pending=[str(uuid.uuid4())])
    for rid in (already_null, real):
        await _set_updated_at(rid, stamp)

    assert await _heal_non_actionable_prune_marker(already_null) is False
    assert await _heal_non_actionable_prune_marker(real) is False

    async with TestSessionLocal() as db:
        rows = (
            await db.execute(
                select(Reservation.id, Reservation.updated_at).where(
                    Reservation.id.in_([already_null, real])
                )
            )
        ).all()
    assert {r.updated_at.replace(tzinfo=None) for r in rows} == {stamp.replace(tzinfo=None)}
    assert await _stored(already_null) == SQL_NULL
    assert await _stored(real) != SQL_NULL


# --- Identity is the UUID, never its spelling (#1554) ---------------------------------
#
# Spellings of one id that the stdlib `uuid.UUID` parser accepts, which is the parser
# on both sides here (`_parse_prune_marker_entry` and the reconciler): case, braces,
# the `urn:uuid:` and `uuid:` prefixes, 32 hex digits, and dashes anywhere. Not every
# spelling it accepts (it also strips any number of braces), but every form it
# normalizes. No writer at main stores anything but the canonical form; these are
# seeded the way a hand edit or an older process would.


def _spellings(device_id: uuid.UUID) -> dict[str, str]:
    canonical = str(device_id)
    hex32 = device_id.hex
    return {
        "lowercase": canonical,
        "uppercase": canonical.upper(),
        "braces": "{" + canonical + "}",
        "braces_uppercase": "{" + canonical.upper() + "}",
        "urn": "urn:uuid:" + canonical,
        "hex32": hex32,
        "hex32_uppercase": hex32.upper(),
        # Accepted by the stdlib parser only; pydantic refuses these (RES-DYN-7).
        "uuid_prefix": "uuid:" + canonical,
        "urn_without_uuid": "urn:" + canonical,
        "urn_braces": "urn:uuid:{" + canonical + "}",
        "braces_hex32": "{" + hex32 + "}",
        "dashes_moved": hex32[:4] + "-" + hex32[4:20] + "-" + hex32[20:],
    }


_SPELLING_KINDS = sorted(_spellings(uuid.uuid4()))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _SPELLING_KINDS)
async def test_a_spelled_entry_is_pruned_once_then_cleared_and_leaves_the_batch(kind):
    """RES-FORK-17 and RES-FORK-20 (issue #1554): an ACTIVE row whose marker holds one
    id in `kind` is pruned once with the canonical id, and the successful prune clears
    the entry, so the column is SQL NULL and the row leaves the selection; a second
    tick makes no call. Before the fix the clear compared spellings, so every spelling
    but lowercase stayed and was pruned again on every tick."""
    dut = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, pending=[_spellings(dut)[kind]])
    calls: list = []

    with _recording_cabling(calls):
        await _run_pending_prune_reconcile()
        await _run_pending_prune_reconcile()

    assert calls == [("POST", f"/internal/forks/{rid}/prune-devices", {"device_ids": [str(dut)]})]
    assert await _stored(rid) == SQL_NULL
    assert await _selected() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _SPELLING_KINDS)
async def test_the_clear_matches_entries_by_uuid_and_keeps_the_rest(kind):
    """The fast-path clear (issue #1554): clearing the canonical id removes its entry
    in `kind`, keeps another id and an entry that does not parse, and a later clear by
    the unparsed entry's text removes it (the reconciler's bad-entry clear)."""
    pruned, other = uuid.uuid4(), uuid.uuid4()
    rid = await _insert(
        ReservationStatus.ACTIVE, pending=[_spellings(pruned)[kind], str(other), "not-a-uuid"]
    )

    await _clear_pending_fork_prune(rid, [pruned])
    assert json.loads(await _stored(rid)) == [str(other), "not-a-uuid"]

    await _clear_pending_fork_prune(rid, ["not-a-uuid"])
    assert json.loads(await _stored(rid)) == [str(other)]

    await _clear_pending_fork_prune(rid, [_spellings(other)[kind]])
    assert await _stored(rid) == SQL_NULL


@pytest.mark.asyncio
async def test_a_terminal_row_with_spelled_entries_clears_to_sql_null():
    """A terminal row's whole marker, every spelling of one id and an entry that does
    not parse, clears in one tick with no cabling call."""
    dut = uuid.uuid4()
    rid = await _insert(
        ReservationStatus.COMPLETED, pending=[*_spellings(dut).values(), "not-a-uuid"]
    )
    calls: list = []

    with _recording_cabling(calls):
        await _run_pending_prune_reconcile()

    assert calls == []
    assert await _stored(rid) == SQL_NULL


# --- The PATCH union treats a stored marker that is not a list of UUIDs as empty -------

_CORRUPT_FOR_PATCH = {
    # shape: (stored raw text, the type name the discard log carries)
    "five": ("5", "int"),
    "true": ("true", "bool"),
    "false": ("false", "bool"),
    "zero": ("0", "int"),
    "empty_string": ('""', "str"),
    "string": ('"marker-text"', "str"),
    "object": ('{"marker-text": 1}', "dict"),
    "empty_object": ("{}", "dict"),
    "list_of_object": ('[{"k": "marker-text"}]', "list"),
    "list_of_int": ("[7]", "list"),
    "list_of_null": ("[null]", "list"),
    "list_of_list": ('[["marker-text"]]', "list"),
    "list_of_non_uuid_string": ('["marker-text"]', "list"),
}


async def _patch_remove(rid: uuid.UUID, keep: str) -> int:
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=_fetch_devices),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        # The fast-path prune would clear the marker; held back so the stored union is
        # what the test reads.
        patch(
            "app.services.reservation_service._prune_removed_devices_from_fork_best_effort",
            new=AsyncMock(),
        ),
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.patch(f"/{rid}", json={"device_ids": [keep]})
    app.dependency_overrides.clear()
    return resp.status_code


def _discard_records(caplog):
    return [
        r
        for r in caplog.records
        if getattr(r, "action", "") == "pending_prune_corrupt_marker_discarded"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_CORRUPT_FOR_PATCH))
async def test_a_patch_over_a_corrupt_marker_answers_200_and_stores_only_the_removed_ids(
    caplog, shape
):
    """RES-PATCH-11 (issue #1554, owner decision): a device-set PATCH that removes a
    device from an ACTIVE row whose stored marker is not a list of UUID strings
    answers 200, stores exactly the removed id, and logs ONE ERROR with its own
    `action` and the stored type's name, never its text. Before the fix `5`, `true`,
    and a list holding an object answered 500 and a string or an object was split
    into junk entries."""
    raw, marker_type = _CORRUPT_FOR_PATCH[shape]
    keep, remove = str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, remove])
    await _store_raw(rid, raw)
    caplog.set_level("INFO", logger="app.services.reservation_service")

    assert await _patch_remove(rid, keep) == 200

    assert json.loads(await _stored(rid)) == [remove]
    records = _discard_records(caplog)
    # Each corrupt list here holds one bad entry; a non-list value is discarded whole.
    discarded_entries = 1 if marker_type == "list" else None
    assert [
        (r.levelname, r.reservation_id, r.marker_type, r.discarded_entries) for r in records
    ] == [("ERROR", str(rid), marker_type, discarded_entries)]
    assert records[0].getMessage() == (
        f"Reservation {rid} held a corrupt pending fork-prune marker of type {marker_type}; "
        "the device-set edit kept only its UUID entries and added the removed devices"
    )
    assert "marker-text" not in caplog.text
    assert "marker-text" not in repr(vars(records[0]))


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["null", "[]"], ids=["json_null", "empty_list"])
async def test_a_patch_over_an_empty_marker_stores_the_removed_ids_and_logs_nothing(caplog, raw):
    """A JSON null or an empty list is an empty marker, not a corrupt one: the PATCH
    stores the removed id and logs no discard."""
    keep, remove = str(uuid.uuid4()), str(uuid.uuid4())
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, remove])
    await _store_raw(rid, raw)
    caplog.set_level("INFO", logger="app.services.reservation_service")

    assert await _patch_remove(rid, keep) == 200

    assert json.loads(await _stored(rid)) == [remove]
    assert _discard_records(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _SPELLING_KINDS)
async def test_a_patch_union_keeps_stored_ids_in_canonical_form_once(caplog, kind):
    """The union compares ids, not spellings (issue #1554): stored entries in `kind`
    (a pending id, and the id being removed now) are written back canonical, each
    once, beside the removed id; the two stored entries that do not parse are dropped
    and logged as one discard of a `list` that counts them."""
    keep, remove = str(uuid.uuid4()), uuid.uuid4()
    pending = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, str(remove)])
    stored = [_spellings(pending)[kind], "not-a-uuid", _spellings(remove)[kind], 7]
    await _store_raw(rid, json.dumps(stored))
    caplog.set_level("INFO", logger="app.services.reservation_service")

    assert await _patch_remove(rid, keep) == 200

    assert json.loads(await _stored(rid)) == sorted([str(pending), str(remove)])
    assert [(r.marker_type, r.discarded_entries) for r in _discard_records(caplog)] == [("list", 2)]


@pytest.mark.asyncio
async def test_a_patch_union_of_valid_canonical_entries_logs_nothing(caplog):
    """The common case is unchanged: a canonical stored marker unions with the removed
    id and nothing is logged as discarded."""
    keep, remove, pending = (str(uuid.uuid4()) for _ in range(3))
    rid = await _insert(ReservationStatus.ACTIVE, device_ids=[keep, remove], pending=[pending])
    caplog.set_level("INFO", logger="app.services.reservation_service")

    assert await _patch_remove(rid, keep) == 200

    assert json.loads(await _stored(rid)) == sorted([pending, remove])
    assert _discard_records(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,parsed",
    [('"abc"', "abc"), ('{"k": 1}', {"k": 1}), ("5", 5)],
    ids=["string", "object", "number"],
)
async def test_the_clear_leaves_a_marker_that_is_not_a_list_for_the_heal(caplog, raw, parsed):
    """RES-FORK-20 (issue #1554): the clear touches only a non-empty list. A stored
    string, object, or number is left exactly as stored (the reconciler's heal owns
    it), even when the pruned ids name what iterating it would yield: a string is not
    rewritten as its characters, nor an object as its keys, and a number does not
    make the clear fail."""
    rid = await _insert(ReservationStatus.ACTIVE, pending=[str(uuid.uuid4())])
    await _store_raw(rid, raw)
    caplog.set_level("INFO", logger="app.services.reservation_service")

    await _clear_pending_fork_prune(rid, ["a", "k", uuid.uuid4()])

    # SQLite hands a stored JSON number back as a number even through the Text cast.
    assert json.loads(str(await _stored(rid))) == parsed
    clear_failed = [
        r
        for r in caplog.records
        if getattr(r, "action", "") == "reservation_fork_prune_marker_clear_failed"
    ]
    assert clear_failed == []
