"""Tests for the reservation auto-expiration background task."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.reservation import Reservation, ReservationStatus
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(hours=2)
FUTURE = NOW + timedelta(hours=2)
FAR_FUTURE = NOW + timedelta(hours=4)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert_reservation(
    status: ReservationStatus,
    start_time: datetime,
    end_time: datetime,
    device_ids: list[str] | None = None,
) -> uuid.UUID:
    """Insert a reservation directly into the database and return its id."""
    res_id = uuid.uuid4()
    if device_ids is None:
        device_ids = [str(uuid.uuid4())]
    async with TestSessionLocal() as session:
        res = Reservation(
            id=res_id,
            user_id=uuid.uuid4(),
            device_ids=device_ids,
            topology_type="PHYSICAL",
            purpose="test",
            start_time=start_time,
            end_time=end_time,
            status=status,
        )
        session.add(res)
        await session.commit()
    return res_id


async def _get_reservation(res_id: uuid.UUID) -> Reservation:
    from sqlalchemy import select

    async with TestSessionLocal() as session:
        result = await session.execute(select(Reservation).where(Reservation.id == res_id))
        return result.scalar_one()


@pytest.mark.asyncio
async def test_expiration_activates_pending():
    """PENDING with past start_time becomes ACTIVE."""
    res_id = await _insert_reservation(ReservationStatus.PENDING, PAST, FUTURE)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.ACTIVE


@pytest.mark.asyncio
async def test_expiration_completes_expired_active():
    """ACTIVE with past end_time becomes COMPLETED."""
    res_id = await _insert_reservation(ReservationStatus.ACTIVE, PAST - timedelta(hours=2), PAST)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.COMPLETED


@pytest.mark.asyncio
async def test_expiration_releases_devices_on_completion():
    """Verify _update_device_statuses called with device_ids and AVAILABLE."""
    device_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    await _insert_reservation(
        ReservationStatus.ACTIVE, PAST - timedelta(hours=2), PAST, device_ids=device_ids
    )
    mock_update = AsyncMock()
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=mock_update,
    ):
        await _run_expiration_cycle()
    mock_update.assert_called_once()
    called_ids, called_status = mock_update.call_args[0]
    assert set(str(d) for d in called_ids) == set(device_ids)
    assert called_status == "AVAILABLE"


@pytest.mark.asyncio
async def test_expiration_ignores_cancelled():
    """CANCELLED stays CANCELLED."""
    res_id = await _insert_reservation(ReservationStatus.CANCELLED, PAST, PAST)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.CANCELLED


@pytest.mark.asyncio
async def test_expiration_ignores_future_pending():
    """PENDING with future start_time stays PENDING."""
    res_id = await _insert_reservation(ReservationStatus.PENDING, FUTURE, FAR_FUTURE)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.PENDING


@pytest.mark.asyncio
async def test_expiration_ignores_completed():
    """COMPLETED stays COMPLETED."""
    res_id = await _insert_reservation(ReservationStatus.COMPLETED, PAST, PAST)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.COMPLETED


@pytest.mark.asyncio
async def test_expiration_activates_and_completes_in_sequence():
    """Two-phase: PENDING to ACTIVE (cycle 1), then ACTIVE to COMPLETED (cycle 2)."""
    # Start in the past, end in the future: first cycle activates only
    res_id = await _insert_reservation(ReservationStatus.PENDING, PAST, FUTURE)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.ACTIVE
    # Now manually set end_time to past to simulate time passing
    from sqlalchemy import update

    async with TestSessionLocal() as session:
        await session.execute(
            update(Reservation).where(Reservation.id == res_id).values(end_time=PAST)
        )
        await session.commit()
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.COMPLETED


@pytest.mark.asyncio
async def test_expiration_does_not_complete_future_active():
    """ACTIVE with future end stays ACTIVE."""
    res_id = await _insert_reservation(ReservationStatus.ACTIVE, PAST, FUTURE)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(res_id)
    assert res.status == ReservationStatus.ACTIVE


@pytest.mark.asyncio
async def test_expiration_empty_database():
    """No reservations, no errors."""
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    # No exception means success


@pytest.mark.asyncio
async def test_expiration_handles_multiple():
    """One past-start PENDING + one past-end ACTIVE: both transition in one cycle."""
    pending_id = await _insert_reservation(ReservationStatus.PENDING, PAST, FUTURE)
    active_id = await _insert_reservation(ReservationStatus.ACTIVE, PAST - timedelta(hours=2), PAST)
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=AsyncMock(),
    ):
        await _run_expiration_cycle()
    pending = await _get_reservation(pending_id)
    active = await _get_reservation(active_id)
    assert pending.status == ReservationStatus.ACTIVE
    assert active.status == ReservationStatus.COMPLETED


@pytest.mark.asyncio
async def test_expiration_releases_all_device_ids_without_exclusivity_check():
    """Bug documentation: expiration releases ALL device_ids without checking exclusivity,
    unlike cancel/release which partition by exclusive flag."""
    device_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    await _insert_reservation(
        ReservationStatus.ACTIVE, PAST - timedelta(hours=2), PAST, device_ids=device_ids
    )
    mock_update = AsyncMock()
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=mock_update,
    ):
        await _run_expiration_cycle()
    # Expiration calls with ALL device_ids (no exclusivity partitioning)
    mock_update.assert_called_once()
    called_ids = [str(d) for d in mock_update.call_args[0][0]]
    assert set(called_ids) == set(device_ids)


@pytest.mark.asyncio
async def test_expiration_no_device_release_when_only_activation():
    """When only PENDING reservations are activated (no completions),
    _update_device_statuses_internal should not be called."""
    await _insert_reservation(ReservationStatus.PENDING, PAST, FUTURE)
    mock_update = AsyncMock()
    with patch(
        "app.tasks.expiration._update_device_statuses",
        new=mock_update,
    ):
        await _run_expiration_cycle()
    mock_update.assert_not_called()
