"""Read endpoints for the device_health_status snapshot.

Snapshot data only. History is exposed via the existing /runs endpoints
since each poll produces three ExecutionRun rows. Path prefix
/device-health avoids collision with the service-level /health used as
the liveness probe.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from herd_common.auth import make_auth_dependencies
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.device_health_status import DeviceHealthStatus
from app.schemas.health import DeviceHealthStatusResponse, PaginatedDeviceHealthResponse
from app.services import device_visibility

logger = logging.getLogger(__name__)

get_current_user_payload, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(tags=["health"])


@router.get("/device-health/{device_id}", response_model=DeviceHealthStatusResponse)
async def get_device_health(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
):
    """Return the current health snapshot for a device.

    On miss, returns a synthesized UNKNOWN record rather than 404 so the
    frontend health badge does not need per-device error handling on
    devices that have not been polled yet (poll_interval_seconds=NULL,
    or scheduler hasn't ticked since the device was added).

    A non-admin may read only a device inside their device-group visibility
    (issue #1108), resolved through the one execution visibility helper; any
    other device answers with the same synthesized record an unknown id gets,
    so the answer says nothing about whether the device exists or was polled.
    An unanswerable visibility lookup is 503. Admins are unfiltered.
    """
    visible = await device_visibility.resolve_caller_visibility(payload, authorization)
    row = None
    if visible is None or device_id in visible:
        row = await db.get(DeviceHealthStatus, device_id)
    if row is None:
        return DeviceHealthStatusResponse(
            device_id=device_id,
            last_polled_at=None,
            last_status="UNKNOWN",
            last_run_id=None,
            consecutive_failures=0,
            next_poll_at=None,
        )
    return row


@router.get("/device-health", response_model=PaginatedDeviceHealthResponse)
async def list_device_health(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    last_status: str | None = Query(None),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List health snapshots, admin only. Future dashboard endpoint."""
    query = select(DeviceHealthStatus)
    if last_status is not None:
        query = query.where(DeviceHealthStatus.last_status == last_status)
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    if total == 0 and last_status not in (
        None,
        "UNKNOWN",
        "HEALTHY",
        "DEGRADED",
        "UNREACHABLE",
    ):
        # Distinguishing an empty result from an invalid filter helps
        # the frontend surface validation errors instead of "no devices".
        raise HTTPException(
            status_code=422,
            detail=f"Invalid last_status: {last_status}",
        )
    result = await db.execute(
        query.order_by(DeviceHealthStatus.last_polled_at.desc().nulls_last())
        .offset(skip)
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return PaginatedDeviceHealthResponse(items=rows, total=total, skip=skip, limit=limit)
