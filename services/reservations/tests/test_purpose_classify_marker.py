"""purpose_classify_requested_at is stamped at all five terminal-transition
sites (issue #646 phase 2, ADR 0013 point 8), the same five sites that
archive the fork best-effort (test_fork_archive_reconcile.py is the template
this file mirrors). Idempotency (an already-set marker is never overwritten)
is also pinned here.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.services.reservation_service import (
    apply_provision_result,
    cancel_reservation,
    release_reservation,
)
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy.ext.asyncio import async_sessionmaker

# The expiration cycle opens its own AsyncSessionLocal against the app engine,
# so this suite must share that engine (mirrors test_expiration.py and
# test_fork_archive_reconcile.py).
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


async def _insert(
    status: ReservationStatus,
    *,
    user_id: uuid.UUID = USER_ID,
    end_offset_h: float = 2,
    updated_at: datetime | None = None,
    dynamic: bool = False,
    purpose_classify_requested_at: datetime | None = None,
) -> uuid.UUID:
    res = Reservation(
        user_id=user_id,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_type=TopologyType.PHYSICAL,
        purpose="t",
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=end_offset_h),
        status=status,
        purpose_classify_requested_at=purpose_classify_requested_at,
    )
    if dynamic:
        res.dynamic_requests = [ReservationDynamicRequest(template_id=uuid.uuid4())]
    if updated_at is not None:
        res.updated_at = updated_at
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _get(rid: uuid.UUID) -> Reservation:
    async with TestSessionLocal() as db:
        return await db.get(Reservation, rid)


# --- Site 1: cancel_reservation -------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_reservation_stamps_marker():
    rid = await _insert(ReservationStatus.ACTIVE)
    with patch(
        "app.services.reservation_service._fetch_devices_best_effort",
        AsyncMock(return_value=[]),
    ):
        async with TestSessionLocal() as db:
            await cancel_reservation(db, rid, USER_ID)
    res = await _get(rid)
    assert res.status == ReservationStatus.CANCELLED
    assert res.purpose_classify_requested_at is not None


# --- Site 2: release_reservation -------------------------------------------------------


@pytest.mark.asyncio
async def test_release_reservation_stamps_marker():
    rid = await _insert(ReservationStatus.ACTIVE)
    with patch(
        "app.services.reservation_service._fetch_devices_best_effort",
        AsyncMock(return_value=[]),
    ):
        async with TestSessionLocal() as db:
            await release_reservation(db, rid, USER_ID)
    res = await _get(rid)
    assert res.status == ReservationStatus.COMPLETED
    assert res.purpose_classify_requested_at is not None


# --- Site 3: apply_provision_result failure branch -------------------------------------


@pytest.mark.asyncio
async def test_provision_result_failed_stamps_marker():
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    with patch(
        "app.services.reservation_service._release_exclusive_devices_best_effort",
        AsyncMock(),
    ):
        async with TestSessionLocal() as db:
            _, applied = await apply_provision_result(
                db, rid, succeeded=False, device_ids=[], error="boom"
            )
    assert applied is True
    res = await _get(rid)
    assert res.status == ReservationStatus.FAILED
    assert res.purpose_classify_requested_at is not None


@pytest.mark.asyncio
async def test_provision_result_success_does_not_stamp_marker():
    """ACTIVE is not terminal: the success branch must not stamp the marker."""
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    with patch(
        "app.services.reservation_service._create_reservation_fork_best_effort",
        AsyncMock(),
    ):
        async with TestSessionLocal() as db:
            _, applied = await apply_provision_result(
                db, rid, succeeded=True, device_ids=[], error=None
            )
    assert applied is True
    res = await _get(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert res.purpose_classify_requested_at is None


# --- Site 4: expiration cycle auto-complete ---------------------------------------------


@pytest.mark.asyncio
async def test_expiry_autocomplete_stamps_marker():
    rid = await _insert(ReservationStatus.ACTIVE, end_offset_h=-1)  # already expired
    with (
        patch("app.tasks.expiration._fetch_devices_best_effort", AsyncMock(return_value=[])),
        patch("app.tasks.expiration._update_device_statuses", AsyncMock()),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_expiration_cycle()
    res = await _get(rid)
    assert res.status == ReservationStatus.COMPLETED
    assert res.purpose_classify_requested_at is not None


# --- Site 5: expiration cycle dynamic-timeout failure backstop --------------------------


@pytest.mark.asyncio
async def test_timeout_backstop_failed_stamps_marker():
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION,
        dynamic=True,
        updated_at=NOW - timedelta(hours=1),  # older than provision_timeout deadline
    )
    with (
        patch("app.tasks.expiration._release_exclusive_devices_best_effort", AsyncMock()),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_expiration_cycle()
    res = await _get(rid)
    assert res.status == ReservationStatus.FAILED
    assert res.purpose_classify_requested_at is not None


# --- Idempotency: an already-set marker is never overwritten ---------------------------


@pytest.mark.asyncio
async def test_stamp_is_idempotent_on_cancel():
    already_requested = NOW - timedelta(days=1)
    rid = await _insert(ReservationStatus.ACTIVE, purpose_classify_requested_at=already_requested)
    with patch(
        "app.services.reservation_service._fetch_devices_best_effort",
        AsyncMock(return_value=[]),
    ):
        async with TestSessionLocal() as db:
            await cancel_reservation(db, rid, USER_ID)
    res = await _get(rid)
    # SQLite (the test backend) drops tzinfo on round-trip; compare naive.
    assert res.purpose_classify_requested_at.replace(tzinfo=None) == already_requested.replace(
        tzinfo=None
    )
