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
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.reservation import Reservation, ReservationStatus
from app.tasks.expiration import _run_reminder_cycle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)


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
async def test_reminder_emitted_once_within_window():
    res_id = await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
    pub.assert_awaited_once()
    subject = pub.await_args[0][1]
    event = pub.await_args[0][2]
    assert subject == "herd.reservations.expiring_soon"
    assert event["event"] == "reservation.expiring_soon"
    assert event["reservation_id"] == str(res_id)
    assert "end_time" in event and "user_id" in event
    assert await _reminder_sent_at(res_id) is not None


@pytest.mark.asyncio
async def test_reminder_not_repeated_across_ticks():
    """Exactly one event across two cycles for a reservation in the window."""
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
            await _run_reminder_cycle(nats)
    assert pub.await_count == 1


@pytest.mark.asyncio
async def test_reminder_skips_outside_window():
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(hours=5))
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_skips_already_expired():
    """end_time already in the past is the completion path, not a reminder."""
    await _insert(ReservationStatus.ACTIVE, NOW - timedelta(minutes=5))
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_skips_non_active():
    await _insert(ReservationStatus.PENDING, NOW + timedelta(minutes=30))
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_skips_already_reminded():
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30), reminded=True)
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_lead_window_zero_disables_reminder():
    await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    nats = AsyncMock()
    with _patched_lead(0):
        with patch("app.tasks.expiration._publish_nats_event", new=AsyncMock()) as pub:
            await _run_reminder_cycle(nats)
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_stamps_before_publish_so_failed_publish_not_retried():
    """The row is stamped even if the publish fails, so a transient NATS error
    does not produce a duplicate on the next tick (at-most-once)."""
    res_id = await _insert(ReservationStatus.ACTIVE, NOW + timedelta(minutes=30))
    nats = AsyncMock()
    with _patched_lead(3600):
        with patch(
            "app.tasks.expiration._publish_nats_event",
            new=AsyncMock(side_effect=RuntimeError("nats down")),
        ):
            # _publish_nats_event swallows its own errors in production; here we
            # force it to raise to prove the stamp already happened. The cycle
            # itself should let it propagate is fine since the loop wraps it.
            with pytest.raises(RuntimeError):
                await _run_reminder_cycle(nats)
    assert await _reminder_sent_at(res_id) is not None
