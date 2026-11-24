"""Restart backstop releases the devices of the row it reverts (issue #993).

The physical-only restart backstop (issue #318) moves a row stranded in
PENDING_PROVISION back to PENDING. Under the hold rule (issue #897) a PENDING row
holds nothing, so every later path (cancel, the elapsed-window failure) writes no
inventory status for it. The backstop must therefore release the devices the
stranded attempt set RESERVED, holder-aware, and only when its own compare-and-swap
won. Uses app.database's own engine because the sweep opens its sessions from it.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import app.tasks.expiration as exp
import pytest
from app.database import Base, engine
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationDynamicRequest, ReservationStatus
from app.services import reservation_service as svc
from app.services.reservation_service import cancel_reservation
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)
EXP = "app.tasks.expiration"
SVC = "app.services.reservation_service"
OWNER = uuid.uuid4()
STALE = NOW - timedelta(hours=2)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _reservation(
    status,
    device_ids,
    *,
    start=None,
    end=None,
    updated_at=None,
    provision_started_at=None,
    dynamic=0,
):
    """Build one unsaved reservation; `dynamic` attaches that many dynamic requests."""
    res = Reservation(
        id=uuid.uuid4(),
        user_id=OWNER,
        device_ids=[str(d) for d in device_ids],
        topology_type="PHYSICAL",
        purpose="test",
        start_time=start or NOW - timedelta(minutes=30),
        end_time=end or NOW + timedelta(hours=2),
        status=status,
        provision_started_at=provision_started_at,
    )
    if updated_at is not None:
        res.updated_at = updated_at
    if dynamic:
        res.dynamic_requests = [
            ReservationDynamicRequest(template_id=uuid.uuid4()) for _ in range(dynamic)
        ]
    return res


async def _insert(status, device_ids, **fields):
    """Insert and commit one reservation built by `_reservation`; return its id."""
    res = _reservation(status, device_ids, **fields)
    async with TestSessionLocal() as session:
        session.add(res)
        await session.commit()
    return res.id


async def _status(rid):
    async with TestSessionLocal() as session:
        return (
            await session.execute(select(Reservation.status).where(Reservation.id == rid))
        ).scalar_one()


async def _subjects():
    async with TestSessionLocal() as session:
        rows = (await session.execute(select(OutboxEvent))).scalars().all()
    return sorted(r.subject for r in rows)


class _Spy:
    """Records every inventory status write, in order, across both modules."""

    def __init__(self):
        self.calls: list[tuple[list[str], str]] = []

    async def __call__(self, ids, status, **_):
        self.calls.append(([str(i) for i in ids], status))
        return list(ids)

    def writes_for(self, device_id):
        return [st for ids, st in self.calls if str(device_id) in ids]


@pytest.fixture
def spy():
    s = _Spy()

    async def exclusive(ids):
        return [{"id": str(i), "exclusive": True} for i in ids]

    with (
        patch(f"{EXP}._update_device_statuses", new=s),
        patch(f"{SVC}._update_device_statuses", new=s),
        patch(f"{EXP}._fetch_devices_best_effort", new=exclusive),
        patch(f"{SVC}._fetch_devices_best_effort", new=exclusive),
        patch(f"{EXP}._create_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{EXP}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
    ):
        yield s


async def test_restart_backstop_revert_releases_the_rows_devices(spy):
    dev = uuid.uuid4()
    rid = await _insert(ReservationStatus.PENDING_PROVISION, [dev], updated_at=STALE)
    await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.PENDING
    assert spy.calls == [([str(dev)], "AVAILABLE")]


async def test_restart_backstop_revert_then_elapsed_window_releases_exactly_once(spy):
    """A start-now booking shorter than the timeout: the revert releases, then the
    next tick fails the elapsed PENDING row with no inventory call (issue #898)."""
    dev = uuid.uuid4()
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION,
        [dev],
        start=NOW - timedelta(hours=3),
        end=NOW - timedelta(minutes=1),
        updated_at=STALE,
    )
    await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.PENDING
    await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.FAILED
    assert spy.writes_for(dev) == ["AVAILABLE"]
    assert await _subjects() == ["herd.reservations.failed"]


async def test_restart_backstop_revert_then_cancel_leaves_nothing_reserved(spy):
    dev = uuid.uuid4()
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION,
        [dev],
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=3),
        updated_at=STALE,
    )
    await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.PENDING
    async with TestSessionLocal() as db:
        await cancel_reservation(db, rid, OWNER, "tok")
    assert await _status(rid) == ReservationStatus.CANCELLED
    # The cancel of a PENDING row writes nothing; the revert's release is the
    # last word, so the device is not left RESERVED.
    assert spy.writes_for(dev) == ["AVAILABLE"]


async def test_restart_backstop_release_skips_a_device_another_live_row_holds(spy):
    held, free = uuid.uuid4(), uuid.uuid4()
    rid = await _insert(ReservationStatus.PENDING_PROVISION, [held, free], updated_at=STALE)
    await _insert(ReservationStatus.ACTIVE, [held])
    await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.PENDING
    assert spy.calls == [([str(free)], "AVAILABLE")]


async def test_restart_backstop_lost_cas_releases_nothing(spy):
    """An in-process activation that committed ACTIVE between the SELECT and the
    conditional write wins the row; the backstop must not release its devices."""
    dev = uuid.uuid4()
    rid = await _insert(ReservationStatus.PENDING_PROVISION, [dev], updated_at=STALE)
    real_claim = svc._claim_provision_transition

    async def racing_claim(db, reservation_id, new_status):
        await db.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id)
            .values(status=ReservationStatus.ACTIVE)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await real_claim(db, reservation_id, new_status)

    with patch.object(exp, "_claim_provision_transition", new=racing_claim):
        await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.ACTIVE
    assert spy.calls == []


# RES-SWEEP-7 judges each stranded row by ITS OWN dynamic requests (issue #1531). Each
# case seeds the stranded physical-only row next to another reservation that carries a
# dynamic request; that request must not hide the stranded row, and the other row ends
# exactly where its own rules put it. With the restart backstop's NOT EXISTS correlation
# inverted or uncorrelated, the other row's request would hide the stranded one. The
# `own_request_stranded` row is stranded too but carries its own request, so it takes
# the dynamic backstop (RES-SWEEP-6, FAILED) and is never reverted to PENDING. A NOT
# EXISTS deleted outright hides nothing; the race test after this one pins that case.
# Each value: (seeded status, updated_at or None for now, final status, its device's
# inventory writes).
_OTHER_ROWS = {
    "active": (ReservationStatus.ACTIVE, None, ReservationStatus.ACTIVE, []),
    "pending_provision_inside_deadline": (
        ReservationStatus.PENDING_PROVISION,
        None,
        ReservationStatus.PENDING_PROVISION,
        [],
    ),
    "own_request_stranded": (
        ReservationStatus.PENDING_PROVISION,
        STALE,
        ReservationStatus.FAILED,
        ["AVAILABLE"],
    ),
}


@pytest.mark.parametrize("other", sorted(_OTHER_ROWS))
async def test_restart_backstop_judges_each_row_by_its_own_dynamic_requests(spy, other):
    seeded, updated_at, final, other_writes = _OTHER_ROWS[other]
    stranded_dev, other_dev = uuid.uuid4(), uuid.uuid4()
    stranded = await _insert(ReservationStatus.PENDING_PROVISION, [stranded_dev], updated_at=STALE)
    other_rid = await _insert(seeded, [other_dev], updated_at=updated_at, dynamic=1)

    await _run_expiration_cycle()

    assert await _status(stranded) == ReservationStatus.PENDING
    assert spy.writes_for(stranded_dev) == ["AVAILABLE"]
    assert await _status(other_rid) == final
    assert spy.writes_for(other_dev) == other_writes
    assert len(spy.calls) == 1 + len(other_writes)
    async with TestSessionLocal() as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
    failed = [str(other_rid)] if final == ReservationStatus.FAILED else []
    assert [(e.subject, e.payload["reservation_id"]) for e in events] == [
        ("herd.reservations.failed", rid) for rid in failed
    ]


async def test_restart_backstop_leaves_a_dynamic_row_that_appears_after_the_dynamic_select(spy):
    """A dynamic-carrying PENDING_PROVISION row whose provision clock is already past
    the deadline can become visible between the dynamic backstop's SELECT and the
    restart backstop's SELECT: under READ COMMITTED each statement takes a new
    snapshot, the dynamic backstop awaits its compare-and-swap UPDATEs in between,
    and the create path stamps `provision_started_at` with the booking time before
    its commit, so a late commit (or clock skew) lands such a row in that gap. The
    restart backstop must leave it alone, judged by its own dynamic request, so
    RES-SWEEP-6 fails it on the next tick; reverting it to PENDING would release its
    devices while execution may be provisioning it.

    SQLite has no concurrent writer, so the other transaction's commit is modelled by
    adding the row on the sweep's own session and flushing it, on the dynamic
    backstop's first compare-and-swap (the only claim to FAILED this tick), after
    that backstop's SELECT and before the restart backstop's. A stranded physical-only
    row is the control: the restart backstop does run after the late row appears."""
    trigger_dev, stranded_dev, late_dev = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    trigger = await _insert(
        ReservationStatus.PENDING_PROVISION, [trigger_dev], updated_at=STALE, dynamic=1
    )
    stranded = await _insert(ReservationStatus.PENDING_PROVISION, [stranded_dev], updated_at=STALE)
    late = _reservation(
        ReservationStatus.PENDING_PROVISION, [late_dev], provision_started_at=STALE, dynamic=1
    )
    real_claim = svc._claim_provision_transition
    added: list[uuid.UUID] = []

    async def claim_after_a_late_commit(db, reservation_id, new_status):
        if new_status == ReservationStatus.FAILED and not added:
            db.add(late)
            await db.flush()
            added.append(late.id)
        return await real_claim(db, reservation_id, new_status)

    with patch.object(exp, "_claim_provision_transition", new=claim_after_a_late_commit):
        await _run_expiration_cycle()

    assert added == [late.id]
    assert await _status(late.id) == ReservationStatus.PENDING_PROVISION
    assert spy.writes_for(late_dev) == []
    assert await _status(trigger) == ReservationStatus.FAILED
    assert spy.writes_for(trigger_dev) == ["AVAILABLE"]
    assert await _status(stranded) == ReservationStatus.PENDING
    assert spy.writes_for(stranded_dev) == ["AVAILABLE"]
    assert len(spy.calls) == 2
    async with TestSessionLocal() as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
    assert [(e.subject, e.payload["reservation_id"]) for e in events] == [
        ("herd.reservations.failed", str(trigger))
    ]
