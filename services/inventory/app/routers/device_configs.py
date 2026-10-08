import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.device_config import (
    ConfigValidationError,
    PublishedSchemaError,
    validate_device_config,
    validate_device_config_with_schema,
)
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.models.device import Device
from app.models.device_config_version import DeviceConfigVersion
from app.schemas.device_config import (
    DeviceConfigApplyResponse,
    DeviceConfigDiff,
    DeviceConfigRestoreRequest,
    DeviceConfigVersionCreate,
    DeviceConfigVersionDetail,
    DeviceConfigVersionResponse,
    PaginatedDeviceConfigVersions,
)
from app.services.apply_outcome import judge_success_answer, move_current_config_pointer
from app.services.config_diff import render_unified_diff
from app.services.device_visibility import check_device_read_visibility
from app.services.manage_guard import (
    _assert_driver_can_configure,
    _is_admin,
    _user_can_manage_device,
)
from app.services.published_schema import published_schema_for_device
from app.services.reservation_guard import find_blocking_reservations_for_device
from app.services.template_service import _integrity_kind

logger = logging.getLogger(__name__)

router = APIRouter(tags=["device-configs"])


async def _load_device(db: AsyncSession, device_id: uuid.UUID) -> Device:
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _connection_type_for(device: Device) -> str:
    template = device.template
    driver = template.driver if template else None
    if not driver or not driver.connection_type:
        raise HTTPException(
            status_code=422,
            detail="Device has no driver-defined connection_type; cannot validate config",
        )
    return driver.connection_type


async def _validate_config_for_device(
    device: Device,
    connection_type: str,
    config: dict | None,
) -> None:
    """Validate a device config, preferring the driver-published schema.

    Resolution order (issue #23):
      1. driver-published schema (proxied from execution), if any, else
      2. the hardcoded CONFIG_SCHEMAS registry (today's behavior).

    published_schema_for_device already fails open to None when execution is
    unreachable, so an outage degrades to the registry rather than 503-ing the
    write. A published schema that is itself unsafe or invalid raises
    PublishedSchemaError from the validator; we log it and fall back to the
    registry too, never breaking the write. Raises ConfigValidationError (mapped
    to 422 by the caller) on a genuine validation failure.
    """
    published = await published_schema_for_device(device)
    if published is not None:
        try:
            validate_device_config_with_schema(
                connection_type, config, schema=published, role=device.name
            )
            return
        except PublishedSchemaError as exc:
            logger.warning(
                "Driver-published schema for device %s is unusable (%s); "
                "falling back to the registry",
                device.name,
                exc,
            )
    validate_device_config(connection_type, config, role=device.name)


async def _load_version(
    db: AsyncSession, device_id: uuid.UUID, version_id: uuid.UUID
) -> DeviceConfigVersion:
    version = await db.get(DeviceConfigVersion, version_id)
    if not version or version.device_id != device_id:
        raise HTTPException(status_code=404, detail="Config version not found")
    return version


@router.get(
    "/devices/{device_id}/config-versions",
    response_model=PaginatedDeviceConfigVersions,
)
async def list_config_versions(
    device_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    await _load_device(db, device_id)
    await check_device_read_visibility(db, device_id, payload, authorization)

    count = (
        await db.execute(
            select(func.count())
            .select_from(DeviceConfigVersion)
            .where(DeviceConfigVersion.device_id == device_id)
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                select(DeviceConfigVersion)
                .where(DeviceConfigVersion.device_id == device_id)
                .order_by(DeviceConfigVersion.version_number.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return PaginatedDeviceConfigVersions(
        items=[DeviceConfigVersionResponse.model_validate(r) for r in rows],
        total=count,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/devices/{device_id}/config-versions/diff",
    response_model=DeviceConfigDiff,
)
async def diff_config_versions(
    device_id: uuid.UUID,
    a: uuid.UUID = Query(..., alias="from"),
    b: uuid.UUID = Query(..., alias="to"),
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    await _load_device(db, device_id)
    await check_device_read_visibility(db, device_id, payload, authorization)
    va = await _load_version(db, device_id, a)
    vb = await _load_version(db, device_id, b)
    diff = render_unified_diff(
        va.config,
        vb.config,
        label_a=f"v{va.version_number}",
        label_b=f"v{vb.version_number}",
    )
    return DeviceConfigDiff(version_a=va.id, version_b=vb.id, diff=diff)


@router.get(
    "/devices/{device_id}/config-versions/latest/internal",
    response_model=DeviceConfigVersionDetail,
)
async def get_latest_config_version_internal(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Get a device's latest config version. Internal service-to-service endpoint.

    Serves the execution service's L3 route provisioning (issue #20): the NATS
    consumer has no acting user, so it cannot call the JWT-gated config-version
    endpoints. Latest means highest version_number, deliberately NOT the
    current_config_version_id pointer, which tracks what a configure action
    last applied rather than the newest validated intent.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    await _load_device(db, device_id)
    version = (
        await db.execute(
            select(DeviceConfigVersion)
            .where(DeviceConfigVersion.device_id == device_id)
            .order_by(DeviceConfigVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="No config versions for device")
    return DeviceConfigVersionDetail.model_validate(version)


@router.get(
    "/devices/{device_id}/config-versions/{version_id}",
    response_model=DeviceConfigVersionDetail,
)
async def get_config_version(
    device_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    await _load_device(db, device_id)
    await check_device_read_visibility(db, device_id, payload, authorization)
    version = await _load_version(db, device_id, version_id)
    return DeviceConfigVersionDetail.model_validate(version)


async def _next_version_number(db: AsyncSession, device_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.max(DeviceConfigVersion.version_number)).where(
                DeviceConfigVersion.device_id == device_id
            )
        )
    ).scalar()
    return (current or 0) + 1


# Each retry recomputes max+1 against the committed rows, so a small cap absorbs
# realistic contention on one device; exhaustion means something else is wrong.
# Same shape as cabling's version_service._commit_with_new_version.
_MAX_VERSION_ALLOCATE_ATTEMPTS = 5

# Pinned (issue #1095): tests match on this exact string.
VERSION_ALLOCATION_CONFLICT_DETAIL = (
    "Could not allocate a config version number under concurrent writes; retry the request"
)


async def _commit_new_version(
    db: AsyncSession, device_id: uuid.UUID, version: DeviceConfigVersion
) -> None:
    """Number `version` max+1 for its device and commit, retrying on a collision.

    Issue #1095. The read of max+1 and the insert are not atomic, so two concurrent
    writers for one device can pick the same number. The unique index
    ix_device_config_versions_device_version (declared on the model, ensured by
    migration 0023) is the arbiter: the loser's commit raises a unique violation,
    rolls back, recomputes max+1 against the winner's committed row, and tries
    again. Only a unique violation is retried; any other integrity error
    propagates. Past the cap the caller gets 409
    VERSION_ALLOCATION_CONFLICT_DETAIL rather than a 500 or a duplicate number.
    """
    for attempt in range(_MAX_VERSION_ALLOCATE_ATTEMPTS):
        version.version_number = await _next_version_number(db, device_id)
        db.add(version)
        try:
            await db.commit()
            return
        except IntegrityError as exc:
            await db.rollback()
            if _integrity_kind(exc) != "unique":
                raise
            logger.info(
                "config version number %s for device %s collided (attempt %d)",
                version.version_number,
                device_id,
                attempt + 1,
                extra={"action": "config_version_number_collision"},
            )
    logger.warning(
        "config version allocation for device %s exhausted %d attempts",
        device_id,
        _MAX_VERSION_ALLOCATE_ATTEMPTS,
        extra={"action": "config_version_allocation_exhausted"},
    )
    raise HTTPException(status_code=409, detail=VERSION_ALLOCATION_CONFLICT_DETAIL)


@router.post(
    "/devices/{device_id}/config-versions",
    response_model=DeviceConfigVersionDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_config_version(
    device_id: uuid.UUID,
    body: DeviceConfigVersionCreate,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    device = await _load_device(db, device_id)

    # ACL gate: writing a new config version is a write that materially affects
    # how the device gets configured the next time something fires it, so we
    # require `manage` (matching the apply path). Admins bypass.
    if not _is_admin(payload):
        allowed = await _user_can_manage_device(payload["sub"], device_id, authorization)
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "manage permission required on this device (or active reservation ownership)"
                ),
            )

    connection_type = _connection_type_for(device)

    try:
        await _validate_config_for_device(device, connection_type, body.config)
    except ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    version = DeviceConfigVersion(
        device_id=device_id,
        connection_type=connection_type,
        config=body.config,
        description=body.description,
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    # `device.current_config_version_id` is intentionally NOT flipped here.
    # The pointer means "what is actually applied", and a draft creation does
    # not apply anything. `apply_config_version` flips it after a successful
    # run.
    await _commit_new_version(db, device_id, version)
    await db.refresh(version)
    return DeviceConfigVersionDetail.model_validate(version)


@router.post(
    "/devices/{device_id}/config-versions/{version_id}/restore",
    response_model=DeviceConfigVersionDetail,
    status_code=status.HTTP_201_CREATED,
)
async def restore_config_version(
    device_id: uuid.UUID,
    version_id: uuid.UUID,
    body: DeviceConfigRestoreRequest,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    device = await _load_device(db, device_id)

    if not _is_admin(payload):
        allowed = await _user_can_manage_device(payload["sub"], device_id, authorization)
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "manage permission required on this device (or active reservation ownership)"
                ),
            )

    source = await _load_version(db, device_id, version_id)

    # Issue #337: block a restore that would pull config out from under an
    # active reservation someone else holds, mirroring topology restore's
    # active-reservation lock (services/cabling/app/routes/versions.py). A
    # reservation the caller owns is exempt: `_user_can_manage_device` above
    # already treats owning an active reservation on this device as
    # equivalent to `manage`, so a reservation holder restoring their own
    # device mid-reservation is the self-service case that ACL widening
    # exists for, not the surprise-rollback case this guard targets.
    blocking = await find_blocking_reservations_for_device(device_id)
    blocking_others = [b for b in blocking if str(b.get("user_id") or "") != payload["sub"]]
    if blocking_others:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Device has active reservations; restore blocked",
                "reservations": [
                    {
                        "id": str(b.get("id")),
                        "status": b.get("status"),
                        "end_time": b.get("end_time"),
                    }
                    for b in blocking_others
                ],
            },
        )

    # Re-validate the source config against the CURRENT schema before restoring.
    # The driver-published schema may have tightened since the source version
    # was written (e.g. the driver file was replaced), so a restore is a fresh
    # write that must satisfy today's schema, not the one in force historically.
    try:
        await _validate_config_for_device(device, source.connection_type, source.config)
    except ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    description = body.description
    if description is None:
        description = f"Restored from v{source.version_number}"

    new_version = DeviceConfigVersion(
        device_id=device_id,
        connection_type=source.connection_type,
        config=source.config,
        description=description,
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
        restored_from_id=source.id,
    )
    # See note in create_config_version: restore writes a draft, it does not
    # apply, so the current-config pointer stays where it was.
    await _commit_new_version(db, device_id, new_version)
    await db.refresh(new_version)
    return DeviceConfigVersionDetail.model_validate(new_version)


@router.post(
    "/devices/{device_id}/config-versions/{version_id}/apply",
    response_model=DeviceConfigApplyResponse,
)
async def apply_config_version(
    device_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    device = await _load_device(db, device_id)
    version = await _load_version(db, device_id, version_id)

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
    # before the call to execution below, so an apply that can never succeed
    # is refused up front rather than reaching the runner. Raises 409 with a
    # structured detail on refusal.
    _assert_driver_can_configure(device)

    url = f"{settings.execution_service_url.rstrip('/')}/execute"
    body = {
        "device_id": str(device.id),
        "action": "configure",
        "user_id": payload["sub"],
        "method_kwargs": version.config,
    }
    headers = {"Authorization": authorization} if authorization else {}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return DeviceConfigApplyResponse(
            version_id=version.id,
            run_id=None,
            status="failed",
            error=f"execution service unreachable: {exc}",
        )

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        return DeviceConfigApplyResponse(
            version_id=version.id,
            run_id=None,
            status="failed",
            error=f"{resp.status_code} {detail}",
        )

    # One success rule for both apply paths (issue #1094, apply_outcome): a 2xx
    # is a success only with a JSON object whose run status is SUCCESS, so a
    # missing status or a body that is not JSON is a failure here too.
    outcome = judge_success_answer(resp)

    if outcome.succeeded or outcome.run_id is not None:
        version.last_apply_run_id = outcome.run_id
        # Only a successful apply moves the device's current-config pointer; a
        # failed one leaves it on whatever was applied last (or NULL if none).
        # The scheduled path moves it by the same helper.
        if outcome.succeeded:
            await move_current_config_pointer(db, device.id, version.id)
        await db.commit()

    return DeviceConfigApplyResponse(
        version_id=version.id,
        run_id=outcome.run_id,
        status=outcome.run_status,
        error=outcome.error,
    )
