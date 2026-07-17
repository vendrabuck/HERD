"""reservation.wiring_changed staging: the ledger anchor and the sweeper heal (#345 P3b).

ADR 0007 Decision 2 and 3. Covers, at the reservations boundary:

- The atomicity invariant: the outbox row exists if and only if the ledger advanced.
  A failure between the two writes commits neither.
- The save-path payload shape, including the per-wire fields relayed verbatim from
  cabling, and the ledger advancing to the saved version.
- Heal events carry released/built as null (the load-bearing full-reconcile marker);
  save events carry arrays.
- The sweeper stages a heal when cabling's latest fork_version exceeds the ledger
  (including a missing ledger row, treated as 0), stages nothing when in sync, and
  isolates a cabling fetch failure from the rest of the sweep.
- The save handler stages on a successful relay and stages nothing on the ARCHIVED 409.

The expiration sweep and stage_wiring_changed open their own AsyncSessionLocal against
the app engine, so this suite shares that engine (mirrors test_fork_archive_reconcile).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.fork_wiring_ledger import ForkWiringLedger
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from app.services import reservation_service
from app.services.reservation_service import WIRING_CHANGED_SUBJECT, stage_wiring_changed
from app.tasks.expiration import _run_fork_archive_reconcile
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert(status: ReservationStatus, *, user_id: uuid.UUID = USER_ID) -> uuid.UUID:
    res = Reservation(
        user_id=user_id,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_type=TopologyType.PHYSICAL,
        purpose="t",
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=2),
        status=status,
    )
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _wiring_rows() -> list[OutboxEvent]:
    async with TestSessionLocal() as db:
        return (
            (
                await db.execute(
                    select(OutboxEvent).where(OutboxEvent.subject == WIRING_CHANGED_SUBJECT)
                )
            )
            .scalars()
            .all()
        )


async def _ledger(reservation_id: uuid.UUID) -> ForkWiringLedger | None:
    async with TestSessionLocal() as db:
        return await db.get(ForkWiringLedger, reservation_id)


def _wire() -> dict:
    return {
        "device_a_id": str(uuid.uuid4()),
        "port_a": "a0",
        "device_b_id": str(uuid.uuid4()),
        "port_b": "b0",
        "layer": "L1",
        "physical_connection_id": str(uuid.uuid4()),
    }


# --- stage_wiring_changed: atomicity and payload shape -------------------------------


@pytest.mark.asyncio
async def test_atomicity_neither_persists_on_failure_between_writes():
    """The invariant, pinned: outbox row exists iff the ledger advanced.

    enqueue_event stages the outbox row first, then the ledger upsert raises before the
    single commit. Nothing is committed, so after a rollback neither row persists.
    """
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        with patch.object(
            reservation_service,
            "_upsert_wiring_ledger",
            new=AsyncMock(side_effect=RuntimeError("boom between writes")),
        ):
            with pytest.raises(RuntimeError):
                await stage_wiring_changed(db, rid, 2, released=[], built=[])
        await db.rollback()

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None


@pytest.mark.asyncio
async def test_save_staging_payload_and_ledger_advance():
    """A save stages one event with the exact payload and per-wire fields, ledger to N."""
    rid = uuid.uuid4()
    built = [_wire()]
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 2, released=[], built=built)

    rows = await _wiring_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.subject == "herd.reservations.wiring_changed"
    payload = row.payload
    assert payload["event"] == "reservation.wiring_changed"
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 2
    assert payload["released"] == []
    assert payload["built"] == built
    # enqueue_event stamps a stable event_id, and it matches the outbox row id.
    assert payload["event_id"] == str(row.id)
    # Every per-wire identity field survives verbatim.
    wire = payload["built"][0]
    assert set(wire) == {
        "device_a_id",
        "port_a",
        "device_b_id",
        "port_b",
        "layer",
        "physical_connection_id",
    }

    ledger = await _ledger(rid)
    assert ledger is not None
    assert ledger.last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_heal_staging_carries_null_delta():
    """A heal event carries released/built as null, not empty lists (Decision 2)."""
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 5, released=None, built=None)

    payload = (await _wiring_rows())[0].payload
    assert payload["released"] is None
    assert payload["built"] is None
    assert payload["fork_version"] == 5
    assert (await _ledger(rid)).last_staged_fork_version == 5


@pytest.mark.asyncio
async def test_upsert_advances_existing_ledger_row():
    """A second staging on an existing ledger advances it rather than inserting."""
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=1))
        await db.commit()
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 3, released=None, built=None)
    assert (await _ledger(rid)).last_staged_fork_version == 3


# --- Sweeper heal via _run_fork_archive_reconcile -----------------------------------


def _forks(*pairs: tuple[uuid.UUID, int]):
    return patch(
        "app.tasks.expiration._fetch_active_forks",
        AsyncMock(return_value=list(pairs)),
    )


@pytest.mark.asyncio
async def test_heal_stages_when_cabling_exceeds_missing_ledger():
    """A missing ledger row counts as 0, so an ACTIVE fork at v2 heals to v2."""
    rid = await _insert(ReservationStatus.ACTIVE)
    with (
        _forks((rid, 2)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 2
    assert payload["released"] is None
    assert payload["built"] is None
    assert (await _ledger(rid)).last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_heal_stages_when_cabling_exceeds_existing_ledger():
    """An ACTIVE fork whose ledger sits behind cabling heals to cabling's latest."""
    rid = await _insert(ReservationStatus.ACTIVE)
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=1))
        await db.commit()

    with (
        _forks((rid, 3)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    rows = await _wiring_rows()
    assert len(rows) == 1
    assert rows[0].payload["fork_version"] == 3
    assert (await _ledger(rid)).last_staged_fork_version == 3


@pytest.mark.asyncio
async def test_no_heal_when_in_sync():
    """Ledger equal to cabling's latest stages nothing and leaves the ledger untouched."""
    rid = await _insert(ReservationStatus.ACTIVE)
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=2))
        await db.commit()

    with (
        _forks((rid, 2)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    assert await _wiring_rows() == []
    assert (await _ledger(rid)).last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_terminal_reservation_is_not_healed():
    """A terminal (COMPLETED) reservation is archived, never wiring-healed."""
    rid = await _insert(ReservationStatus.COMPLETED)
    archive = AsyncMock()
    with (
        _forks((rid, 9)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        await _run_fork_archive_reconcile()

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None
    archive.assert_awaited_once_with(rid)


@pytest.mark.asyncio
async def test_heal_isolated_from_cabling_fetch_failure():
    """A cabling fetch failure is non-fatal: the sweep returns cleanly, stages nothing."""
    rid = await _insert(ReservationStatus.ACTIVE)
    with (
        patch(
            "app.tasks.expiration._fetch_active_forks",
            AsyncMock(side_effect=RuntimeError("cabling unreachable")),
        ),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None


# --- Save handler: stages on success, nothing on the ARCHIVED 409 -------------------


def _resp(status_code: int, json_body=None) -> httpx.Response:
    if json_body is None:
        return httpx.Response(status_code)
    return httpx.Response(status_code, json=json_body)


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


@pytest.mark.asyncio
async def test_save_handler_relays_body_and_stages_event():
    """A successful save relays the cabling body verbatim AND stages the event + ledger."""
    rid = await _insert(ReservationStatus.ACTIVE)
    built = [_wire()]
    released = [_wire()]
    save_body = {
        "fork_id": str(uuid.uuid4()),
        "version_number": 4,
        "released": released,
        "built": built,
        "unchanged_count": 1,
    }
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, save_body)),
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {"nodes": []}})

    assert resp.status_code == 200
    # The relay must not lose or change any field of the cabling save response.
    assert resp.json() == save_body

    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["fork_version"] == 4
    assert payload["released"] == released
    assert payload["built"] == built
    assert (await _ledger(rid)).last_staged_fork_version == 4

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_staging_payload_carries_edge_key_verbatim():
    """Per-wire edge_key (issue #345 P3b) passes through the payload unchanged, null too.

    reservations relays cabling's delta wires verbatim, so the canvas edge_key each wire
    carries (or its null when a hop is ungrouped) must survive into the staged
    reservation.wiring_changed payload for the execution consumer to group hops per edge.
    """
    rid = uuid.uuid4()
    grouped = {**_wire(), "edge_key": "edge-42"}
    ungrouped = {**_wire(), "edge_key": None}
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 7, released=[ungrouped], built=[grouped])

    payload = (await _wiring_rows())[0].payload
    assert payload["built"][0]["edge_key"] == "edge-42"
    assert payload["released"][0]["edge_key"] is None


@pytest.mark.asyncio
async def test_save_handler_archived_409_stages_nothing():
    """An ARCHIVED-fork 409 from cabling relays the error and stages neither row."""
    rid = await _insert(ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(409, {"detail": "Fork is archived and cannot be edited"})),
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {}})

    assert resp.status_code == 409
    assert await _wiring_rows() == []
    assert await _ledger(rid) is None

    app.dependency_overrides.clear()
