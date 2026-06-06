import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.acl import user_has_manage_or_owns_active_reservation
from herd_common.device_config import ConfigValidationError, validate_device_config
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
        validate_device_config(connection_type, body.config, role=device.name)
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
    await _load_device(db, device_id)

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
