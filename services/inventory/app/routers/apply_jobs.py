"""Schedule, list, and cancel endpoints for device-config apply jobs.

A job points at a specific `device_config_version` and a `scheduled_for`
timestamp. A background task in `app.services.apply_scheduler` fires due
jobs against the execution service. If a job has a `reservation_id`, the
scheduler verifies the reservation is currently active before firing;
otherwise the job is marked `skipped`.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.models.device import Device
from app.models.device_config_apply_job import DeviceConfigApplyJob
from app.models.device_config_version import DeviceConfigVersion
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.schemas.device_config import (
    ApplyJobResponse,
    ApplyJobScheduleRequest,
    ApplyJobsInternalSummary,
    PaginatedApplyJobs,
)
from app.services.device_visibility import check_device_read_visibility
from app.services.manage_guard import (
    _assert_driver_can_configure,
    _is_admin,
    _user_can_manage_device,
)

APPLY_JOBS_SUMMARY_NAME_CAP = 20

# Pinned: tests match on these exact strings (issues #704 and #1104). The admin
# variant drops "you own": an admin is exempt from ownership, not from the
# activeness or the device check. The style follows execution's
# EXECUTE_RESERVATION_* details (CFG-EXEC-3).
RESERVATION_MISMATCH_ERROR = (
    "reservation_id must reference an active reservation you own that includes this device"
)
RESERVATION_MISMATCH_ADMIN_ERROR = (
    "reservation_id must reference an active reservation that includes this device"
)
RESERVATION_UNAVAILABLE_ERROR = (
    "Could not verify the reservation; nothing was scheduled. Retry the request."
)
_RESERVATIONS_HTTP_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger(__name__)

router = APIRouter(tags=["apply-jobs"])


def _reservation_unavailable(device_id: uuid.UUID, why: str) -> HTTPException:
    """Log why the reservation check could not be answered and build the fixed 503.

    The reason is HERD-authored (a status code or an exception class), never
    upstream text, and it goes to the log only; the caller sees the fixed detail.
    """
    logger.warning("Reservation check for schedule on device %s failed (%s)", device_id, why)
    return HTTPException(status_code=503, detail=RESERVATION_UNAVAILABLE_ERROR)


async def _validate_reservation_for_job(
    *, reservation_id: uuid.UUID, payload: dict, device_id: uuid.UUID
) -> None:
    """Check a caller-supplied reservation_id at schedule time (issues #704, #1104).

    The job carries this id, and the scheduler's fire-time check keys on it
    (CFG-SCHED-5), so it must name a reservation that is ACTIVE now, holds the
    device, and, for a non-admin, belongs to the caller. An admin is exempt
    from ownership only. Two internal-token reads answer that:

    1. GET /internal/{reservation_id}: `is_active` (status ACTIVE and inside
       its window), the same judgement the scheduler makes at fire time. A 404
       or an inactive reservation is a 422.
    2. GET /internal/by-device/{device_id}: every reservation, of any status
       and any owner, whose device set holds the device, with its owner. The
       named id must be listed and, for a non-admin, its `user_id` must be the
       caller; otherwise 422.

    Together these imply what reservations' GET /internal/active answers
    (the caller owns an ACTIVE, in-window reservation holding the device),
    so that third read is no longer made here. The reservation-owner widening
    for a non-admin without a manage grant is the separate authorization
    check in the route (`_user_can_manage_device`) and is unchanged.

    One 422 detail per caller kind, so a non-admin cannot tell an unknown id,
    an inactive one, another user's, and one without the device apart. The
    check fails closed: no internal token, a transport error, a non-200 other
    than the status read's 404, a body that is not JSON, a status body that is
    not an object, or a by-device body that is not a list of objects with
    string `id` and `user_id` is 503 RESERVATION_UNAVAILABLE_ERROR, and
    nothing is scheduled.
    """
    is_admin = _is_admin(payload)
    mismatch = RESERVATION_MISMATCH_ADMIN_ERROR if is_admin else RESERVATION_MISMATCH_ERROR
    if not settings.internal_api_token:
        raise _reservation_unavailable(device_id, "no internal token configured")
    base = settings.reservations_service_url.rstrip("/")
    headers = {"X-Internal-Token": settings.internal_api_token}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{base}/internal/{reservation_id}",
                headers=headers,
                timeout=_RESERVATIONS_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise _reservation_unavailable(device_id, type(exc).__name__) from None
        if resp.status_code == 404:
            raise HTTPException(status_code=422, detail=mismatch)
        if resp.status_code != 200:
            raise _reservation_unavailable(device_id, f"status read answered {resp.status_code}")
        try:
            status_data = resp.json()
        except ValueError:
            status_data = None
        # A 200 whose JSON is not an object is unusable, the same fail-closed
        # 503 as a non-JSON body (issue #1096).
        if not isinstance(status_data, dict):
            raise _reservation_unavailable(device_id, "status read answered a misshapen body")
        if not status_data.get("is_active"):
            raise HTTPException(status_code=422, detail=mismatch)

        try:
            holders_resp = await client.get(
                f"{base}/internal/by-device/{device_id}",
                headers=headers,
                timeout=_RESERVATIONS_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise _reservation_unavailable(device_id, type(exc).__name__) from None
        if holders_resp.status_code != 200:
            raise _reservation_unavailable(
                device_id, f"by-device read answered {holders_resp.status_code}"
            )
        try:
            rows = holders_resp.json()
        except ValueError:
            rows = None
        if not isinstance(rows, list) or not all(
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and isinstance(row.get("user_id"), str)
            for row in rows
        ):
            raise _reservation_unavailable(device_id, "by-device read answered a misshapen body")
    for row in rows:
        try:
            if uuid.UUID(row["id"]) != reservation_id:
                continue
        except ValueError:
            continue
        if is_admin or row["user_id"] == str(payload.get("sub")):
            return
        break
    raise HTTPException(status_code=422, detail=mismatch)


@router.post(
    "/devices/{device_id}/config-versions/{version_id}/schedule",
    response_model=ApplyJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def schedule_apply_job(
    device_id: uuid.UUID,
    version_id: uuid.UUID,
    body: ApplyJobScheduleRequest,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    # Past-time guard: the scheduler will not "catch up" missed runs, so a
    # past timestamp would either fire instantly (surprising) or sit idle as a
    # zombie. Reject up front. Equal-to-now also rejected to avoid clock-skew
    # firing-before-creation races.
    now = datetime.now(timezone.utc)
    scheduled_for = body.scheduled_for
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    if scheduled_for <= now:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="scheduled_for must be in the future",
        )

    # Horizon guard (issue #704): an unbounded scheduling window lets a job
    # sit queued far longer than any reservation window or ACL grant is
    # likely to still be valid by fire time, widening the gap the fire-time
    # authority re-check has to cover.
    max_horizon = timedelta(days=settings.apply_job_max_horizon_days)
    if scheduled_for > now + max_horizon:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"scheduled_for must be within {settings.apply_job_max_horizon_days} days from now"
            ),
        )

    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    version = await db.get(DeviceConfigVersion, version_id)
    if not version or version.device_id != device_id:
        raise HTTPException(status_code=404, detail="Config version not found")

    # ACL gate: non-admins must have `manage` on the device OR own an active
    # reservation containing it (iter-3 widening for the AI write flow). This
    # is schedule-time authorization only: the execution service's own
    # immediate-apply /execute path is STRICTER, requiring a hard ACL manage
    # grant with no reservation widening, so a reservation owner without an
    # explicit grant can schedule here but would be refused on an immediate
    # apply (issue #704). The scheduler re-runs this same check at fire time
    # (apply_scheduler._creator_still_authorized), since a deferred job's
    # authorization can otherwise go stale between scheduling and firing.
    # Without this check, any authenticated user could queue arbitrary configs.
    if not _is_admin(payload):
        allowed = await _user_can_manage_device(payload["sub"], device_id, authorization)
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "manage permission required on this device (or active reservation ownership)"
                ),
            )

    # Driver-capability gate (issue #839): after authorization so an
    # unauthorized caller learns nothing new about the device's driver, and
    # before the reservation/dry-run checks and job creation below, so an
    # apply that can never succeed is refused up front rather than reaching
    # the runner. Raises 409 with a structured detail on refusal.
    _assert_driver_can_configure(device)

    # Reservation-id validation (issues #704, #1104): an optional reservation_id
    # must name an ACTIVE reservation that holds this device and, for a
    # non-admin, belongs to the caller. Without this, a caller could attach an
    # arbitrary, foreign, or unrelated reservation_id to a job, and the
    # fire-time reservation-active check would then follow that reservation
    # instead of the one that authorized the job.
    if body.reservation_id is not None:
        await _validate_reservation_for_job(
            reservation_id=body.reservation_id,
            payload=payload,
            device_id=device_id,
        )

    # Dry-run gate: drivers must opt in via driver_metadata.json. Without this
    # check, scheduling a dry-run against an older driver that ignores
    # context["dry_run"] would push the config for real.
    if body.dry_run:
        template = await db.get(DeviceTemplate, device.template_id)
        driver = await db.get(DriverPackage, template.driver_id) if template else None
        if driver is None or not driver.supports_dry_run:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "this driver does not advertise dry-run support; "
                    "refuse to fire a dry-run that would hit the wire"
                ),
            )

    job = DeviceConfigApplyJob(
        device_id=device_id,
        version_id=version_id,
        scheduled_for=body.scheduled_for,
        reservation_id=body.reservation_id,
        dry_run=body.dry_run,
        status="pending",
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return ApplyJobResponse.model_validate(job)


@router.get(
    "/devices/{device_id}/apply-jobs",
    response_model=PaginatedApplyJobs,
)
async def list_apply_jobs(
    device_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Group-visibility gate (issue #909): a non-admin caller outside the
    # device's groups gets the same 404 an unknown device id gets, so this
    # endpoint cannot be used to confirm a hidden device exists or to read
    # its config-apply history (author names, driver-returned error text).
    # Mirrors device_configs.py's config-version reads (issue #718).
    await check_device_read_visibility(db, device_id, payload, authorization)

    total = (
        await db.execute(
            select(func.count())
            .select_from(DeviceConfigApplyJob)
            .where(DeviceConfigApplyJob.device_id == device_id)
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                select(DeviceConfigApplyJob)
                .where(DeviceConfigApplyJob.device_id == device_id)
                .order_by(DeviceConfigApplyJob.scheduled_for.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return PaginatedApplyJobs(
        items=[ApplyJobResponse.model_validate(r) for r in rows],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/apply-jobs/{job_id}",
    response_model=ApplyJobResponse,
)
async def get_apply_job(
    job_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single apply job by id. Used by the frontend confirmation
    modal to poll until the dry-run is in a terminal state. Authenticated
    user only; visibility through this endpoint matches list_apply_jobs
    (no per-job ACL gate, since the existence of a job_id implies the
    caller already had visibility into it via the listing).

    Group-visibility gate (issue #909): gated on the job's OWN device_id
    rather than a device_id path param, since this route only takes a job
    id. A non-admin whose groups do not cover the job's device gets the same
    404 a nonexistent job_id gets ("Apply job not found"), so this route
    cannot be used to confirm a hidden device's job exists.
    """
    job = await db.get(DeviceConfigApplyJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Apply job not found")
    await check_device_read_visibility(
        db, job.device_id, payload, authorization, not_found_detail="Apply job not found"
    )
    return ApplyJobResponse.model_validate(job)


@router.post(
    "/apply-jobs/{job_id}/confirm",
    response_model=ApplyJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_dry_run_apply(
    job_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Promote a successful dry-run apply into a real apply.

    The AI assistant is forbidden from setting dry_run=False; the human
    confirms via this endpoint after reviewing the captured command
    transcript in the UI. We create a brand-new job rather than flipping
    dry_run on the source row, so the promotion is auditable as a separate
    row by the confirming user at a fresh timestamp.

    Reuses the same ACL gate as schedule_apply_job (manage grant or
    active-reservation owner), so a user cannot promote a dry-run for a device
    they could no longer schedule against. Order: 404 for an unknown job, then
    the authority check (403), then the 409s when the source is not a
    successful dry-run, so a caller without authority learns only that the job
    exists, never its kind or status (issue #1113).
    """
    source = await db.get(DeviceConfigApplyJob, job_id)
    if not source:
        raise HTTPException(status_code=404, detail="Apply job not found")

    if not _is_admin(payload):
        allowed = await _user_can_manage_device(payload["sub"], source.device_id, authorization)
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "manage permission required on this device (or active reservation ownership)"
                ),
            )

    if not source.dry_run:
        raise HTTPException(
            status_code=409,
            detail="Source job is not a dry-run; nothing to promote",
        )
    if source.status != "success":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Source dry-run is {source.status!r}; only successful dry-runs can be promoted"
            ),
        )

    # Create a new job, NOT a flip on the source. Audit attribution is the
    # confirming user; the source job remains the historical record of the
    # dry-run that the confirmation was based on.
    promoted = DeviceConfigApplyJob(
        device_id=source.device_id,
        version_id=source.version_id,
        scheduled_for=datetime.now(timezone.utc) + timedelta(seconds=10),
        reservation_id=source.reservation_id,
        dry_run=False,
        status="pending",
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    db.add(promoted)
    await db.commit()
    await db.refresh(promoted)
    logger.info(
        "apply_job_promoted_from_dry_run",
        extra={
            "source_job_id": str(source.id),
            "source_created_by": str(source.created_by),
            "promoted_job_id": str(promoted.id),
            "promoted_by": payload["sub"],
            "device_id": str(source.device_id),
            "version_id": str(source.version_id),
        },
    )
    return ApplyJobResponse.model_validate(promoted)


@router.delete(
    "/apply-jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_apply_job(
    job_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(DeviceConfigApplyJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Apply job not found")
    if str(job.created_by) != payload["sub"] and not _is_admin(payload):
        raise HTTPException(status_code=403, detail="Not authorized to cancel this job")
    if job.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status!r}, not cancellable",
        )
    # Compare-and-swap (issue #1088): the scheduler's claim in
    # apply_scheduler.fire_job is a conditional update on status='pending' too,
    # so exactly one of the two writes wins the row. A cancel that loses (the
    # claim committed after the read above) changes nothing and answers the same
    # 409 a cancel of a running job gets, so a 204 always means the job will
    # never fire, and a job that fired never reads as cancelled.
    result = await db.execute(
        update(DeviceConfigApplyJob)
        .where(
            DeviceConfigApplyJob.id == job_id,
            DeviceConfigApplyJob.status == "pending",
        )
        .values(status="cancelled")
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount == 0:
        current = (
            await db.execute(
                select(DeviceConfigApplyJob.status).where(DeviceConfigApplyJob.id == job_id)
            )
        ).scalar_one_or_none()
        if current is None:
            raise HTTPException(status_code=404, detail="Apply job not found")
        logger.info(
            "apply job %s cancel lost to a concurrent writer (now %s)",
            job_id,
            current,
            extra={"action": "apply_job_cancel_lost_race"},
        )
        raise HTTPException(
            status_code=409,
            detail=f"Job is {current!r}, not cancellable",
        )


@router.get(
    "/devices/{device_id}/apply-jobs/internal",
    response_model=ApplyJobsInternalSummary,
)
async def get_apply_jobs_summary_internal(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Config-apply job summary for one device. Internal token only.

    Feeds the AI orchestrator's end-of-reservation purpose classifier (issue
    #646 phase 2): `count` is the total number of apply jobs ever scheduled
    against the device, `names` is the deduplicated, non-null set of the
    associated config versions' free-text `description` field, capped at
    APPLY_JOBS_SUMMARY_NAME_CAP. `description` is a human label the
    scheduling user wrote, never the version's `config` JSON, so this
    endpoint cannot leak device configuration contents or credentials by
    construction; see docs/AI_PURPOSE_CLASSIFICATION.md.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")

    count = (
        await db.execute(
            select(func.count())
            .select_from(DeviceConfigApplyJob)
            .where(DeviceConfigApplyJob.device_id == device_id)
        )
    ).scalar() or 0

    names = (
        (
            await db.execute(
                select(DeviceConfigVersion.description)
                .join(
                    DeviceConfigApplyJob,
                    DeviceConfigApplyJob.version_id == DeviceConfigVersion.id,
                )
                .where(
                    DeviceConfigApplyJob.device_id == device_id,
                    DeviceConfigVersion.description.is_not(None),
                )
                .distinct()
                .limit(APPLY_JOBS_SUMMARY_NAME_CAP)
            )
        )
        .scalars()
        .all()
    )

    return ApplyJobsInternalSummary(count=count, names=[n for n in names if n])
