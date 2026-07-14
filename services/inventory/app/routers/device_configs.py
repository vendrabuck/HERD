import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.acl import user_has_manage_or_owns_active_reservation
from herd_common.device_config import (
    ConfigValidationError,
    PublishedSchemaError,
    validate_device_config,
    validate_device_config_with_schema,
)
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
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
from app.services.config_diff import render_unified_diff
from app.services.published_schema import published_schema_for_device
from app.services.reservation_guard import find_blocking_reservations_for_device

logger = logging.getLogger(__name__)

router = APIRouter(tags=["device-configs"])


def _is_admin(payload: dict) -> bool:
    return payload.get("role") in ("admin", "superadmin")


async def _user_can_manage_device(
    user_id: str,
    device_id: uuid.UUID,
    authorization: str | None,
) -> bool:
    """Wrap the shared herd-common helper with this service's URL config.

    Accepts an explicit `manage` grant OR reservation-owner-of-an-active-
    reservation-containing-this-device, per the iter-3 widening documented
    in docs/ROLES.md.
    """
    return await user_has_manage_or_owns_active_reservation(
        user_id=user_id,
        device_id=str(device_id),
        authorization=authorization,
        acl_service_url=settings.acl_service_url,
        reservations_service_url=settings.reservations_service_url,
        internal_api_token=settings.internal_api_token,
    )


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
    db: AsyncSession = Depends(get_db),
):
    await _load_device(db, device_id)

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
    db: AsyncSession = Depends(get_db),
):
    await _load_device(db, device_id)
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
    db: AsyncSession = Depends(get_db),
):
    await _load_device(db, device_id)
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

    version_number = await _next_version_number(db, device_id)
    version = DeviceConfigVersion(
        device_id=device_id,
        version_number=version_number,
        connection_type=connection_type,
        config=body.config,
        description=body.description,
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    db.add(version)
    # `device.current_config_version_id` is intentionally NOT flipped here.
    # The pointer means "what is actually applied", and a draft creation does
    # not apply anything. `apply_config_version` flips it after a successful
    # run.
    await db.commit()
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

    version_number = await _next_version_number(db, device_id)
    new_version = DeviceConfigVersion(
        device_id=device_id,
        version_number=version_number,
        connection_type=source.connection_type,
        config=source.config,
        description=description,
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
        restored_from_id=source.id,
    )
    db.add(new_version)
    # See note in create_config_version: restore writes a draft, it does not
    # apply, so the current-config pointer stays where it was.
    await db.commit()
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

    try:
        data = resp.json()
    except ValueError:
        data = {}
    run_id = data.get("id")
    run_status = str(data.get("status", "SUCCESS")).lower()

    # Parse the run_id BEFORE any DB mutation so a malformed value can never
    # prevent the commit that persists `device.current_config_version_id`. A
    # bad run_id simply degrades to a NULL pointer on the persisted version.
    parsed_run_id: uuid.UUID | None = None
    if run_id:
        try:
            parsed_run_id = uuid.UUID(run_id)
        except (ValueError, TypeError):
            parsed_run_id = None

    if run_id:
        version.last_apply_run_id = parsed_run_id
        # Only move the device's current-config pointer when this apply
        # actually succeeded. A failed apply leaves the pointer on whatever
        # version was applied last (or NULL if none).
        if run_status == "success":
            device.current_config_version_id = version.id
        await db.commit()

    return DeviceConfigApplyResponse(
        version_id=version.id,
        run_id=parsed_run_id,
        status=run_status,
        error=data.get("error"),
    )
