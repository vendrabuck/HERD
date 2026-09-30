"""Sweep-level device-hold invariant (issue #898).

An exclusive device is RESERVED in inventory iff some reservation in
{PENDING_PROVISION, ACTIVE} holds it. Within ONE expiration tick, a completed
predecessor's release must not clobber a same-tick successor's activation
(R2.start == R1.end is legal, so any tick that sees R1.end <= now also sees
R2.start <= now), and a PENDING row whose window already elapsed is failed, never
activated. Uses app.database's own engine like test_expiration.py, because the
sweep opens its sessions from it.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)
EXP = "app.tasks.expiration"
SVC = "app.services.reservation_service"


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert(status, start, end, device_ids):
    rid = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            Reservation(
                id=rid,
                user_id=uuid.uuid4(),
                device_ids=[str(d) for d in device_ids],
                topology_type="PHYSICAL",
                purpose="test",
                start_time=start,
                end_time=end,
                status=status,
            )
        )
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
    ):
        yield s


async def test_adjacent_reservations_due_in_one_tick_end_reserved(spy):
    """R1 ends exactly when R2 starts, both due in one tick: D ends RESERVED, R2 ACTIVE."""
    dev = uuid.uuid4()
    boundary = NOW - timedelta(seconds=40)
    r1 = await _insert(ReservationStatus.ACTIVE, boundary - timedelta(hours=1), boundary, [dev])
    r2 = await _insert(ReservationStatus.PENDING, boundary, NOW + timedelta(hours=1), [dev])
    await _run_expiration_cycle()
    assert await _status(r1) == ReservationStatus.COMPLETED
    assert await _status(r2) == ReservationStatus.ACTIVE
    writes = spy.writes_for(dev)
    assert writes and writes[-1] == "RESERVED"
    assert "AVAILABLE" not in writes
    subjects = await _subjects()
    assert "herd.reservations.completed" in subjects
    assert "herd.reservations.created" in subjects


async def test_release_skips_device_another_active_row_holds(spy):
    """A completed row's release flips only the devices no other live row holds."""
    held, free = uuid.uuid4(), uuid.uuid4()
    await _insert(
        ReservationStatus.ACTIVE, NOW - timedelta(hours=3), NOW - timedelta(hours=1), [held, free]
    )
    await _insert(
        ReservationStatus.ACTIVE, NOW - timedelta(hours=1), NOW + timedelta(hours=1), [held]
    )
    await _run_expiration_cycle()
    assert spy.calls == [([str(free)], "AVAILABLE")]


async def test_release_skips_device_a_pending_provision_row_holds(spy):
    held = uuid.uuid4()
    await _insert(
        ReservationStatus.ACTIVE, NOW - timedelta(hours=3), NOW - timedelta(hours=1), [held]
    )
    await _insert(
        ReservationStatus.PENDING_PROVISION,
        NOW - timedelta(minutes=1),
        NOW + timedelta(hours=1),
        [held],
    )
    await _run_expiration_cycle()
    assert spy.calls == []


async def test_release_ignores_pending_and_terminal_rows_as_holders(spy):
    """PENDING holds nothing (issue #897) and terminal rows hold nothing."""
    dev = uuid.uuid4()
    await _insert(
        ReservationStatus.ACTIVE, NOW - timedelta(hours=3), NOW - timedelta(hours=1), [dev]
    )
    await _insert(
        ReservationStatus.PENDING, NOW + timedelta(hours=1), NOW + timedelta(hours=2), [dev]
    )
    await _insert(
        ReservationStatus.CANCELLED, NOW - timedelta(hours=1), NOW + timedelta(hours=2), [dev]
    )
    await _run_expiration_cycle()
    assert spy.calls == [([str(dev)], "AVAILABLE")]


async def test_expired_pending_row_is_failed_not_activated(spy):
    dev = uuid.uuid4()
    rid = await _insert(
        ReservationStatus.PENDING, NOW - timedelta(hours=2), NOW - timedelta(hours=1), [dev]
    )
    with patch(f"{EXP}._create_reservation_fork_best_effort", new=AsyncMock()) as fork:
        await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.FAILED
    assert await _subjects() == ["herd.reservations.failed"]
    assert spy.calls == []
    fork.assert_not_called()
    async with TestSessionLocal() as session:
        row = (await session.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()
    assert row.purpose_classify_requested_at is not None


async def test_expired_pending_row_logs_fixed_action(spy, caplog):
    await _insert(
        ReservationStatus.PENDING,
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        [uuid.uuid4()],
    )
    with caplog.at_level("WARNING"):
        await _run_expiration_cycle()
    assert [
        r.action for r in caplog.records if getattr(r, "action", "") == "reservation_window_elapsed"
    ]


async def test_pending_row_still_inside_its_window_is_still_activated(spy):
    dev = uuid.uuid4()
    rid = await _insert(
        ReservationStatus.PENDING, NOW - timedelta(minutes=1), NOW + timedelta(hours=1), [dev]
    )
    await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.ACTIVE
    assert spy.calls == [([str(dev)], "RESERVED")]
