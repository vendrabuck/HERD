"""Sweep-side status compare-and-swap loser paths (issue #899).

Scheduled activation (`_activate_pending_reservation`) and the sweep's
auto-complete each move a row with a CAS on the status they expect to leave; a
loser is a clean no-op. The winner's write is committed on a second session at the
moment the loser is mid-flight. Uses app.database's own engine like
test_expiration.py, because the sweep opens its sessions from it.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus
from app.services import reservation_service as svc
from app.tasks.expiration import _activate_pending_reservation, _run_expiration_cycle
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)
EXP = "app.tasks.expiration"
SVC = "app.services.reservation_service"
DEV = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert(status, start, end):
    rid = uuid.uuid4()
    async with TestSessionLocal() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=uuid.uuid4(),
                device_ids=[str(DEV)],
                topology_type="PHYSICAL",
                purpose="t",
                start_time=start,
                end_time=end,
                status=status,
            )
        )
        await s.commit()
    return rid


async def _status(rid):
    async with TestSessionLocal() as s:
        return (
            await s.execute(select(Reservation.status).where(Reservation.id == rid))
        ).scalar_one()


async def _subjects():
    async with TestSessionLocal() as s:
        return sorted(r.subject for r in (await s.execute(select(OutboxEvent))).scalars().all())


async def _set_status(rid, status):
    async with TestSessionLocal() as s:
        await s.execute(update(Reservation).where(Reservation.id == rid).values(status=status))
        await s.commit()


async def _exclusive(ids):
    return [{"id": str(i), "exclusive": True} for i in ids]


class Inventory:
    def __init__(self, on_reserved=None):
        self.calls: list[tuple[list[str], str]] = []
        self.on_reserved = on_reserved

    async def __call__(self, ids, status, **_):
        self.calls.append(([str(i) for i in ids], status))
        if status == "RESERVED" and self.on_reserved is not None:
            await self.on_reserved()
        return list(ids)


@pytest.fixture
def fork():
    mock = AsyncMock()
    with (
        patch(f"{EXP}._fetch_devices_best_effort", new=_exclusive),
        patch(f"{EXP}._create_reservation_fork_best_effort", new=mock),
        patch(f"{EXP}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
    ):
        yield mock


async def test_scheduled_activation_loses_to_cancel_during_flip(fork):
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION, NOW - timedelta(minutes=1), NOW + timedelta(hours=1)
    )

    async def cancel_now():
        await _set_status(rid, ReservationStatus.CANCELLED)

    inv = Inventory(on_reserved=cancel_now)
    with (
        patch(f"{EXP}._update_device_statuses", new=inv),
        patch(f"{SVC}._update_device_statuses", new=inv),
    ):
        assert await _activate_pending_reservation(rid) is False
    assert await _status(rid) == ReservationStatus.CANCELLED
    assert await _subjects() == []
    fork.assert_not_called()
    assert inv.calls == [([str(DEV)], "RESERVED"), ([str(DEV)], "AVAILABLE")]


async def test_scheduled_activation_win_still_activates(fork):
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION, NOW - timedelta(minutes=1), NOW + timedelta(hours=1)
    )
    inv = Inventory()
    with patch(f"{EXP}._update_device_statuses", new=inv):
        assert await _activate_pending_reservation(rid) is True
    assert await _status(rid) == ReservationStatus.ACTIVE
    assert await _subjects() == ["herd.reservations.created"]
    fork.assert_awaited_once()


async def test_auto_complete_loses_to_a_concurrent_release_is_a_noop(fork):
    rid = await _insert(
        ReservationStatus.ACTIVE, NOW - timedelta(hours=3), NOW - timedelta(hours=1)
    )
    real = svc._claim_status_transition

    async def racing(db, reservation_id, expected, new_status):
        await _set_status(rid, ReservationStatus.CANCELLED)
        return await real(db, reservation_id, expected, new_status)

    inv = Inventory()
    with (
        patch(f"{EXP}._update_device_statuses", new=inv),
        patch(f"{EXP}._claim_status_transition", new=racing),
    ):
        await _run_expiration_cycle()
    assert await _status(rid) == ReservationStatus.CANCELLED
    assert await _subjects() == []
    assert inv.calls == []
