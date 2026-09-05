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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
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
from app.services.manage_guard import _is_admin, _user_can_manage_device

APPLY_JOBS_SUMMARY_NAME_CAP = 20

logger = logging.getLogger(__name__)

router = APIRouter(tags=["apply-jobs"])


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
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

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
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single apply job by id. Used by the frontend confirmation
    modal to poll until the dry-run is in a terminal state. Authenticated
    user only; visibility through this endpoint matches list_apply_jobs
    (no per-job ACL gate, since the existence of a job_id implies the
    caller already had visibility into it via the listing).
    """
    job = await db.get(DeviceConfigApplyJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Apply job not found")
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

    Rejects 409 if the source job is not a successful dry-run. Reuses the
    same ACL gate as schedule_apply_job (manage grant or active-reservation
    owner), so a user cannot promote a dry-run for a device they could no
    longer schedule against.
    """
    source = await db.get(DeviceConfigApplyJob, job_id)
    if not source:
        raise HTTPException(status_code=404, detail="Apply job not found")
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

    if not _is_admin(payload):
        allowed = await _user_can_manage_device(payload["sub"], source.device_id, authorization)
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "manage permission required on this device (or active reservation ownership)"
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
    job.status = "cancelled"
    await db.commit()


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
