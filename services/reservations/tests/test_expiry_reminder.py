"""Tests for the upcoming-expiry reminder cycle.

Acceptance criteria covered:
- a reservation within the configured lead window of end_time produces exactly
  one reservation.expiring_soon event;
- the reminder is deduped per reservation across expiration ticks
  (expiry_reminder_sent_at);
- reservations outside the window, already-reminded, or not ACTIVE are skipped;
- a lead window of 0 disables the reminder.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from app.database import Base, engine
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus
from app.tasks.expiration import _run_reminder_cycle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)


async def _expiring_soon_rows():
    """Staged reservation.expiring_soon outbox rows (issue #21)."""
    async with TestSessionLocal() as session:
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.subject == "herd.reservations.expiring_soon")
            .order_by(OutboxEvent.created_at)
        )
        return (await session.execute(stmt)).scalars().all()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert(
    status: ReservationStatus,
    end_time: datetime,
    *,
    reminded: bool = False,
) -> uuid.UUID:
    res_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        res = Reservation(
            id=res_id,
            user_id=uuid.uuid4(),
            device_ids=[str(uuid.uuid4())],
            topology_type="PHYSICAL",
            purpose="test",
            start_time=end_time - timedelta(hours=4),
            end_time=end_time,
            status=status,
            expiry_reminder_sent_at=NOW if reminded else None,
        )
        session.add(res)
        await session.commit()
    return res_id


async def _reminder_sent_at(res_id: uuid.UUID):
    async with TestSessionLocal() as session:
        res = (
            await session.execute(select(Reservation).where(Reservation.id == res_id))
        ).scalar_one()
        return res.expiry_reminder_sent_at


def _patched_lead(seconds: int):
    return patch("app.tasks.expiration.settings.expiry_reminder_lead_seconds", seconds)


@pytest.mark.asyncio
async def test_reminder_staged_once_within_window():
    res_id = await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    with _patched_lead(3600):
        await _run_reminder_cycle()
    rows = await _expiring_soon_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.published_at is None
    event = row.payload
    assert event["event"] == "reservation.expiring_soon"
    assert event["reservation_id"] == str(res_id)
    assert "end_time" in event and "user_id" in event
    assert event["event_id"] == str(row.id)
    assert await _reminder_sent_at(res_id) is not None


@pytest.mark.asyncio
async def test_reminder_not_repeated_across_ticks():
    """Exactly one staged event across two cycles for a reservation in the window.

    The stamp and the outbox row commit together (issue #21), so the second tick
    sees an already-reminded row and stages nothing more.
    """
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    with _patched_lead(3600):
        await _run_reminder_cycle()
        await _run_reminder_cycle()
    assert len(await _expiring_soon_rows()) == 1


@pytest.mark.asyncio
async def test_reminder_skips_outside_window():
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(hours=5))
    with _patched_lead(3600):
        await _run_reminder_cycle()
    assert await _expiring_soon_rows() == []


@pytest.mark.asyncio
async def test_reminder_skips_already_expired():
    """end_time already in the past is the completion path, not a reminder."""
    await _insert(ReservationStatus.ACTIVE, NOW - timedelta(minutes=5))
    with _patched_lead(3600):
        await _run_reminder_cycle()
    assert await _expiring_soon_rows() == []


@pytest.mark.asyncio
async def test_reminder_skips_non_active():
    await _insert(ReservationStatus.PENDING, NOW + timedelta(minutes=30))
    with _patched_lead(3600):
        await _run_reminder_cycle()
    assert await _expiring_soon_rows() == []


@pytest.mark.asyncio
async def test_reminder_skips_already_reminded():
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30), reminded=True)
    with _patched_lead(3600):
        await _run_reminder_cycle()
    assert await _expiring_soon_rows() == []


@pytest.mark.asyncio
async def test_lead_window_zero_disables_reminder():
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    with _patched_lead(0):
        await _run_reminder_cycle()
    assert await _expiring_soon_rows() == []


@pytest.mark.asyncio
async def test_reminder_stamp_and_event_commit_together():
    """The stamp and the outbox event are written in the same transaction (issue
    #21): after one cycle the reservation is stamped AND exactly one unpublished
    expiring_soon row exists, so the relay can never publish a reminder for a row
    that was not stamped, nor stamp one whose event was lost."""
    res_id = await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    with _patched_lead(3600):
        await _run_reminder_cycle()
    rows = await _expiring_soon_rows()
    assert len(rows) == 1
    assert rows[0].published_at is None
    assert rows[0].payload["reservation_id"] == str(res_id)
    assert await _reminder_sent_at(res_id) is not None
