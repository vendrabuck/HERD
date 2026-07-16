"""Fork teardown-archive call sites and the standing archive reconciler (#25 P3a phase 3).

Covers: the best-effort archive is invoked from all five teardown transitions with
retry-and-continue on failure, and the expiration-sweep reconciler archives
terminal-status forks, skips unknown reservation_ids, and survives a fetch failure.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base, engine
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.services import reservation_service
from app.services.reservation_service import (
    _archive_reservation_fork_best_effort,
    apply_provision_result,
    cancel_reservation,
    release_reservation,
)
from app.tasks.expiration import _run_expiration_cycle, _run_fork_archive_reconcile
from sqlalchemy.ext.asyncio import async_sessionmaker

# The expiration cycle and reconciler open their own AsyncSessionLocal against the
# app engine, so this suite must share that engine (mirrors test_expiration.py).
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


# --- Archive invoked from each of the five teardown sites ----------------------------


@pytest.mark.asyncio
async def test_release_reservation_archives_fork():
    rid = await _insert(ReservationStatus.ACTIVE)
    archive = AsyncMock()
    with (
        patch.object(reservation_service, "_fetch_devices_best_effort", AsyncMock(return_value=[])),
        patch.object(reservation_service, "_archive_reservation_fork_best_effort", archive),
    ):
        async with TestSessionLocal() as db:
            await release_reservation(db, rid, USER_ID)
    archive.assert_awaited_once_with(rid)


@pytest.mark.asyncio
async def test_cancel_reservation_archives_fork():
    rid = await _insert(ReservationStatus.ACTIVE)
    archive = AsyncMock()
    with (
        patch.object(reservation_service, "_fetch_devices_best_effort", AsyncMock(return_value=[])),
        patch.object(reservation_service, "_archive_reservation_fork_best_effort", archive),
    ):
        async with TestSessionLocal() as db:
            await cancel_reservation(db, rid, USER_ID)
    archive.assert_awaited_once_with(rid)


@pytest.mark.asyncio
async def test_provision_result_failed_archives_fork():
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    archive = AsyncMock()
    with (
        patch.object(reservation_service, "_release_exclusive_devices_best_effort", AsyncMock()),
        patch.object(reservation_service, "_archive_reservation_fork_best_effort", archive),
    ):
        async with TestSessionLocal() as db:
            _, applied = await apply_provision_result(
                db, rid, succeeded=False, device_ids=[], error="boom"
            )
    assert applied is True
    archive.assert_awaited_once_with(rid)


@pytest.mark.asyncio
async def test_provision_result_success_does_not_archive():
    """The success branch activates and (re)creates the fork; it must not archive."""
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    archive = AsyncMock()
    with (
        patch.object(reservation_service, "_create_reservation_fork_best_effort", AsyncMock()),
        patch.object(reservation_service, "_archive_reservation_fork_best_effort", archive),
    ):
        async with TestSessionLocal() as db:
            _, applied = await apply_provision_result(
                db, rid, succeeded=True, device_ids=[], error=None
            )
    assert applied is True
    archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_autocomplete_archives_fork():
    rid = await _insert(ReservationStatus.ACTIVE, end_offset_h=-1)  # already expired
    archive = AsyncMock()
    with (
        patch("app.tasks.expiration._fetch_devices_best_effort", AsyncMock(return_value=[])),
        patch("app.tasks.expiration._update_device_statuses", AsyncMock()),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        await _run_expiration_cycle()
    archive.assert_awaited_once_with(rid)


@pytest.mark.asyncio
async def test_timeout_backstop_failed_archives_fork():
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION,
        dynamic=True,
        updated_at=NOW - timedelta(hours=1),  # older than provision_timeout deadline
    )
    archive = AsyncMock()
    with (
        patch("app.tasks.expiration._release_exclusive_devices_best_effort", AsyncMock()),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        await _run_expiration_cycle()
    # The stuck dynamic reservation was failed by the backstop, so its fork archives.
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        assert res.status == ReservationStatus.FAILED
    archive.assert_awaited_once_with(rid)


# --- Archive helper: retry-and-continue on failure -----------------------------------


@pytest.mark.asyncio
async def test_archive_best_effort_swallows_and_retries(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "tok")
    inner = AsyncMock(side_effect=RuntimeError("cabling down"))
    with (
        patch.object(reservation_service, "_archive_reservation_fork", inner),
        patch("herd_common.retry.asyncio.sleep", AsyncMock()),
    ):
        # Must not raise even though every attempt fails.
        await _archive_reservation_fork_best_effort(uuid.uuid4())
    assert inner.await_count == 3


@pytest.mark.asyncio
async def test_archive_best_effort_noops_without_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    inner = AsyncMock()
    with patch.object(reservation_service, "_archive_reservation_fork", inner):
        await _archive_reservation_fork_best_effort(uuid.uuid4())
    inner.assert_not_awaited()


# --- Standing reconciler -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_archives_terminal_skips_active_and_unknown():
    completed = await _insert(ReservationStatus.COMPLETED)
    cancelled = await _insert(ReservationStatus.CANCELLED)
    failed = await _insert(ReservationStatus.FAILED)
    active = await _insert(ReservationStatus.ACTIVE)
    unknown = uuid.uuid4()  # cabling reports a fork for a reservation we do not know

    archive = AsyncMock()
    listed = [completed, cancelled, failed, active, unknown]
    with (
        patch(
            "app.tasks.expiration._fetch_active_fork_reservation_ids",
            AsyncMock(return_value=listed),
        ),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        await _run_fork_archive_reconcile()

    archived = {c.args[0] for c in archive.await_args_list}
    assert archived == {completed, cancelled, failed}
    assert active not in archived
    assert unknown not in archived


@pytest.mark.asyncio
async def test_reconcile_survives_fetch_failure():
    archive = AsyncMock()
    with (
        patch(
            "app.tasks.expiration._fetch_active_fork_reservation_ids",
            AsyncMock(side_effect=RuntimeError("cabling unreachable")),
        ),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        # Non-fatal: returns cleanly, archives nothing.
        await _run_fork_archive_reconcile()
    archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_empty_list_noop():
    archive = AsyncMock()
    with (
        patch(
            "app.tasks.expiration._fetch_active_fork_reservation_ids",
            AsyncMock(return_value=[]),
        ),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        await _run_fork_archive_reconcile()
    archive.assert_not_awaited()
