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
from app.models.reservation import Reservation, ReservationStatus
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


async def _insert(status, device_ids, *, start=None, end=None, updated_at=None):
    rid = uuid.uuid4()
    res = Reservation(
        id=rid,
        user_id=OWNER,
        device_ids=[str(d) for d in device_ids],
        topology_type="PHYSICAL",
        purpose="test",
        start_time=start or NOW - timedelta(minutes=30),
        end_time=end or NOW + timedelta(hours=2),
        status=status,
    )
    if updated_at is not None:
        res.updated_at = updated_at
    async with TestSessionLocal() as session:
        session.add(res)
        await session.commit()
    return rid


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
