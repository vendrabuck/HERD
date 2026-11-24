"""Status compare-and-swap loser paths (issue #899).

Every reservation status transition is a compare-and-swap on the status it expects
to leave; the loser must be a clean no-op: no event row, no inventory call, no fork
call, and the row left exactly as the winner wrote it. These tests force the loss
deterministically by committing the winner's write on a SECOND session at the
moment the loser is mid-flight (inside the inventory flip, or just before its CAS).
The real concurrent proof on Postgres is test_reservation_status_cas_live_pg.py.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.schemas.reservation import DynamicRequestSpec, ReservationCreate
from app.services import reservation_service as svc
from app.services.reservation_service import (
    cancel_reservation,
    create_reservation,
    release_reservation,
)
from sqlalchemy import select, update

from tests._harness import TestSessionLocal

SVC = "app.services.reservation_service"
NOW = datetime.now(timezone.utc)
USER_ID = uuid.uuid4()
DEV = uuid.uuid4()
DEV2 = uuid.uuid4()

PENDING = ReservationStatus.PENDING
PROVISION = ReservationStatus.PENDING_PROVISION
ACTIVE = ReservationStatus.ACTIVE
CANCELLED = ReservationStatus.CANCELLED
COMPLETED = ReservationStatus.COMPLETED
FAILED = ReservationStatus.FAILED

# The statuses that end a reservation (docs/specs/reservations.md section 4), stated
# here on their own and NOT read from the product's tuple, so a defect in that tuple
# cannot also bend the test. Every other member of the enum is non-terminal by
# construction: a status added later lands in NON_TERMINAL, where the cancel race
# tests expect it to be retried and cancelled; if the product treats it as terminal
# (lists it in _TERMINAL_STATUSES), those tests fail until it is moved into TERMINAL
# here. A new status listed in neither place is tested as live and passes.
TERMINAL = (COMPLETED, CANCELLED, FAILED)
NON_TERMINAL = tuple(s for s in ReservationStatus if s not in TERMINAL)
# Statuses whose row holds its exclusive devices (issue #897): a cancel that leaves
# one of these releases them, a cancel that leaves anything else does not.
DEVICE_HOLDING = (PROVISION, ACTIVE)


async def _set_status_on_second_session(rid, status):
    async with TestSessionLocal() as other:
        await other.execute(update(Reservation).where(Reservation.id == rid).values(status=status))
        await other.commit()


async def _subjects():
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(OutboxEvent))).scalars().all()
    return sorted(r.subject for r in rows)


async def _row(rid):
    async with TestSessionLocal() as s:
        return (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()


async def _columns(rid):
    """Every mapped column of the row, read on a fresh session."""
    row = await _row(rid)
    return {a.key: getattr(row, a.key) for a in Reservation.__mapper__.column_attrs}


async def _insert(status, device_ids=(DEV,)):
    rid = uuid.uuid4()
    async with TestSessionLocal() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=USER_ID,
                device_ids=[str(d) for d in device_ids],
                topology_type=TopologyType.PHYSICAL,
                purpose="t",
                start_time=NOW + timedelta(hours=1),
                end_time=NOW + timedelta(hours=3),
                status=status,
            )
        )
        await s.commit()
    return rid


class Inventory:
    """Fake inventory status write recording calls; optionally runs a hook first."""

    def __init__(self, on_reserved=None):
        self.calls: list[tuple[list[str], str]] = []
        self.on_reserved = on_reserved

    async def __call__(self, ids, status, *, raise_on_failure=False, succeeded=None):
        self.calls.append(([str(i) for i in ids], status))
        if status == "RESERVED":
            if succeeded is not None:
                succeeded.update(ids)
            if self.on_reserved is not None:
                await self.on_reserved()
        return list(ids)


async def _exclusive(ids):
    return [{"id": str(i), "exclusive": True} for i in ids]


def _devices(ids, token=None):
    return [
        {
            "id": str(i),
            "name": f"d-{str(i)[:4]}",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "exclusive": True,
        }
        for i in ids
    ]


@pytest.fixture
def seams():
    """Stub every HTTP seam of the create, cancel, and release paths."""
    fork = AsyncMock()

    async def fetch(ids, token):
        return _devices(ids)

    async def once(fn, **_):
        return await fn()

    with (
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._fetch_devices_best_effort", new=_exclusive),
        patch(f"{SVC}._create_reservation_fork_best_effort", new=fork),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}._validate_dynamic_requests", new=AsyncMock()),
        patch(f"{SVC}.retry_with_backoff", new=once),
    ):
        yield fork


def _create_body(**kw):
    return ReservationCreate(
        device_ids=[DEV],
        purpose="t",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc) + timedelta(hours=1),
        **kw,
    )


async def _create_row_id(db):
    """The create path's own row id: the only reservation in the table."""
    return (await db.execute(select(Reservation.id))).scalar_one()


# --- create_reservation: the flip window (issue #899 sites 1 and 2) ---


async def test_create_win_activates_stages_created_and_forks(seams):
    inv = Inventory()
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with TestSessionLocal() as db:
            res = await create_reservation(db, _create_body(), USER_ID, "tok")
    assert res.status == ACTIVE
    assert await _subjects() == ["herd.reservations.created"]
    seams.assert_awaited_once()
    assert inv.calls == [([str(DEV)], "RESERVED")]


async def test_create_loses_to_cancel_during_flip_stays_cancelled(seams):
    ids: dict = {}

    async def cancel_now():
        async with TestSessionLocal() as s:
            rid = await _create_row_id(s)
        ids["rid"] = rid
        await _set_status_on_second_session(rid, CANCELLED)

    inv = Inventory(on_reserved=cancel_now)
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with TestSessionLocal() as db:
            res = await create_reservation(db, _create_body(), USER_ID, "tok")
    assert res.status == CANCELLED
    assert (await _row(ids["rid"])).status == CANCELLED
    assert await _subjects() == []
    seams.assert_not_called()
    assert inv.calls == [([str(DEV)], "RESERVED"), ([str(DEV)], "AVAILABLE")]


async def test_create_flip_failure_loses_to_cancel_stays_cancelled(seams):
    ids: dict = {}

    class Boom(Inventory):
        async def __call__(self, ids_, status, **kw):
            await super().__call__(ids_, status, **kw)
            if status == "RESERVED":
                raise RuntimeError("inventory down")
            return list(ids_)

    async def cancel_now():
        async with TestSessionLocal() as s:
            ids["rid"] = await _create_row_id(s)
        await _set_status_on_second_session(ids["rid"], CANCELLED)

    inv = Boom(on_reserved=cancel_now)
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with TestSessionLocal() as db:
            res = await create_reservation(db, _create_body(), USER_ID, "tok")
    assert res.status == CANCELLED
    assert await _subjects() == []
    seams.assert_not_called()
    assert inv.calls[-1] == ([str(DEV)], "AVAILABLE")


async def test_create_dynamic_loses_to_cancel_stages_no_provision_request(seams):
    ids: dict = {}

    async def cancel_now():
        async with TestSessionLocal() as s:
            ids["rid"] = await _create_row_id(s)
        await _set_status_on_second_session(ids["rid"], CANCELLED)

    inv = Inventory(on_reserved=cancel_now)
    body = _create_body(dynamic_requests=[DynamicRequestSpec(template_id=uuid.uuid4())])
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with TestSessionLocal() as db:
            res = await create_reservation(db, body, USER_ID, "tok")
    assert res.status == CANCELLED
    assert await _subjects() == []
    assert inv.calls[-1] == ([str(DEV)], "AVAILABLE")


async def test_lost_create_revert_skips_a_device_a_newer_booking_holds(seams):
    """The revert is holder-aware: the cancel freed the device, a successor took it."""
    ids: dict = {}

    async def cancel_and_rebook():
        async with TestSessionLocal() as s:
            ids["rid"] = await _create_row_id(s)
        await _set_status_on_second_session(ids["rid"], CANCELLED)
        await _insert(ACTIVE, device_ids=(DEV,))

    inv = Inventory(on_reserved=cancel_and_rebook)
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with TestSessionLocal() as db:
            await create_reservation(db, _create_body(), USER_ID, "tok")
    assert inv.calls == [([str(DEV)], "RESERVED")]


# --- cancel_reservation ---


def _racing_claim(rid, winner_status):
    """Wrap the real CAS so the winner's write lands first, on a second session.

    `wrapper.attempts` records each CAS as (expected statuses, new status, won), and
    `wrapper.after_winner` holds every column of the row just after the winner's commit.
    """
    real = svc._claim_status_transition
    fired = {"n": 0}

    async def wrapper(db, reservation_id, expected, new_status):
        if fired["n"] == 0:
            fired["n"] += 1
            await _set_status_on_second_session(rid, winner_status)
            wrapper.after_winner = await _columns(rid)
        expected = tuple(expected)
        won = await real(db, reservation_id, expected, new_status)
        wrapper.attempts.append((expected, new_status, won))
        return won

    wrapper.attempts = []
    wrapper.after_winner = None
    return wrapper


@pytest.mark.parametrize("winner", TERMINAL, ids=lambda s: s.value)
@pytest.mark.parametrize("loaded", NON_TERMINAL, ids=lambda s: f"from_{s.value}")
async def test_cancel_loses_to_any_terminal_winner_is_a_noop(seams, loaded, winner):
    """RES-CANCEL-4: a row a concurrent writer ENDED is left exactly as the winner wrote it.

    The cancel loaded a live row; before its CAS lands, another writer moves the row to
    a terminal status. The cancel loses, re-reads, sees the end, and stops: no second
    CAS (a CANCELLED winner must not be cancelled again, a FAILED one must not be
    overwritten), no event, no field written, no inventory call, no fork archive.
    "No field written" is checked on every mapped column of the row against a
    snapshot taken right after the winner's commit.
    """
    rid = await _insert(loaded)
    inv = Inventory()
    claim = _racing_claim(rid, winner)
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._claim_status_transition", new=claim),
    ):
        async with TestSessionLocal() as db:
            out = await cancel_reservation(db, rid, USER_ID, "tok")
        svc._archive_reservation_fork_best_effort.assert_not_awaited()
    assert out.status == winner
    assert claim.attempts == [((loaded,), CANCELLED, False)]
    assert claim.after_winner["status"] == winner
    assert await _columns(rid) == claim.after_winner
    assert await _subjects() == []
    assert inv.calls == []


@pytest.mark.parametrize(
    ("loaded", "winner"),
    [(a, b) for a in NON_TERMINAL for b in NON_TERMINAL if a != b],
    ids=lambda s: s.value,
)
async def test_cancel_loses_to_a_non_terminal_winner_retries_and_cancels(seams, loaded, winner):
    """RES-CANCEL-4, the complement: a row that only MOVED is still cancelled.

    The winner wrote a live status (for example the restart backstop's revert of
    PENDING_PROVISION to PENDING, RES-SWEEP-7), so the cancel retries from the fresh
    status, wins, and stages exactly one reservation.cancelled; it releases devices
    only if the status it left holds them. A winner that wrote the status the cancel
    loaded is no race at all (the first CAS wins), so those pairs are left out.
    """
    rid = await _insert(loaded)
    inv = Inventory()
    claim = _racing_claim(rid, winner)
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._claim_status_transition", new=claim),
    ):
        async with TestSessionLocal() as db:
            out = await cancel_reservation(db, rid, USER_ID, "tok")
    assert out.status == CANCELLED
    assert claim.attempts == [
        ((loaded,), CANCELLED, False),
        ((winner,), CANCELLED, True),
    ]
    row = await _row(rid)
    assert row.status == CANCELLED
    assert row.modified_by == USER_ID
    assert row.purpose_classify_requested_at is not None
    assert await _subjects() == ["herd.reservations.cancelled"]
    expected_release = [([str(DEV)], "AVAILABLE")] if winner in DEVICE_HOLDING else []
    assert inv.calls == expected_release


async def test_cancel_retries_with_the_fresh_status_when_the_row_moved_forward(seams):
    """PENDING moved to PENDING_PROVISION between load and CAS: cancel it, and release."""
    rid = await _insert(PENDING)
    inv = Inventory()
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._claim_status_transition", new=_racing_claim(rid, PROVISION)),
    ):
        async with TestSessionLocal() as db:
            out = await cancel_reservation(db, rid, USER_ID, "tok")
    assert out.status == CANCELLED
    assert await _subjects() == ["herd.reservations.cancelled"]
    assert inv.calls == [([str(DEV)], "AVAILABLE")]


# --- release_reservation ---


async def test_release_loses_to_a_concurrent_cancel_is_a_noop(seams):
    rid = await _insert(ACTIVE)
    inv = Inventory()
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._claim_status_transition", new=_racing_claim(rid, CANCELLED)),
    ):
        async with TestSessionLocal() as db:
            out = await release_reservation(db, rid, USER_ID, "tok")
    assert out.status == CANCELLED
    assert await _subjects() == []
    assert inv.calls == []


async def test_release_win_completes_stages_one_event_and_releases(seams):
    rid = await _insert(ACTIVE)
    inv = Inventory()
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with TestSessionLocal() as db:
            out = await release_reservation(db, rid, USER_ID, "tok")
    assert out.status == COMPLETED
    assert await _subjects() == ["herd.reservations.completed"]
    assert inv.calls == [([str(DEV)], "AVAILABLE")]


# --- the helper itself ---


async def test_claim_status_transition_matches_only_expected_statuses():
    rid = await _insert(ACTIVE)
    async with TestSessionLocal() as db:
        assert not await svc._claim_status_transition(db, rid, (PENDING, PROVISION), CANCELLED)
        assert await svc._claim_status_transition(db, rid, (PENDING, ACTIVE), CANCELLED)
        assert not await svc._claim_status_transition(db, rid, (ACTIVE,), COMPLETED)
        await db.commit()
    assert (await _row(rid)).status == CANCELLED


async def test_claim_provision_transition_is_the_pending_provision_wrapper():
    a = await _insert(PROVISION)
    b = await _insert(ACTIVE)
    async with TestSessionLocal() as db:
        assert await svc._claim_provision_transition(db, a, ACTIVE)
        assert not await svc._claim_provision_transition(db, b, CANCELLED)
        await db.commit()
    assert (await _row(b)).status == ACTIVE


async def test_cancel_cas_exhaustion_warns_with_fixed_action_and_stages_nothing(seams, caplog):
    """Three lost CASes on a row that stays non-terminal: no silent give-up."""
    rid = await _insert(ACTIVE)
    inv = Inventory()
    claim = AsyncMock(return_value=False)
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._claim_status_transition", new=claim),
        caplog.at_level("WARNING"),
    ):
        async with TestSessionLocal() as db:
            out = await cancel_reservation(db, rid, USER_ID, "tok")
    assert out.status == ACTIVE
    # Three compare-and-swap attempts in all (the first and two retries), then it stops.
    assert claim.await_count == 3
    warned = [
        r for r in caplog.records if getattr(r, "action", "") == "reservation_cancel_cas_exhausted"
    ]
    assert len(warned) == 1
    assert warned[0].reservation_id == str(rid)
    assert warned[0].status == "ACTIVE"
    assert await _subjects() == []
    assert inv.calls == []
