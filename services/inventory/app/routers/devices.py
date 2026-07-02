import logging
import os
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.models.device import Device, DeviceStatus, TopologyType
from app.models.template import DeviceTemplate
from app.schemas.device import DeviceCreate, DeviceResponse, DeviceUpdate, PaginatedDeviceResponse
from app.services.inventory_service import (
    create_device,
    delete_device,
    get_device,
    list_devices,
    set_device_status,
    update_device,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["devices"])

_FAULT_STATUS_SENTINEL = "__herd_fault_status__"

# Sentinel written over password-typed field values on non-admin reads. Matches
# the config service's secret masking so the frontend renders a stable, obviously
# redacted value. The key is retained (only its value is replaced) so the response
# shape is unchanged for the UI.
_REDACTED_VALUE = "********"


def _password_field_keys(template: DeviceTemplate | None) -> set[str]:
    """Bare field_data keys whose template field is type "password".

    Mirrors the execution service's extract_password_keys, but over the bare keys
    used in a device's field_data (execution prefixes them with HERD_ for the run
    context env; inventory stores them unprefixed). Defensive against malformed
    sections so a bad template never breaks a read.
    """
    if template is None or not template.sections:
        return set()
    keys: set[str] = set()
    for section in template.sections:
        if not isinstance(section, dict):
            continue
        for field in section.get("fields", []):
            if isinstance(field, dict) and field.get("type") == "password":
                key = field.get("key")
                if key:
                    keys.add(key)
    return keys


def _redact_field_data(field_data: dict, password_keys: set[str]) -> dict:
    """Replace truthy password-typed values with the redaction sentinel.

    Empty/None values are left untouched so a redacted response never implies a
    secret exists where none was set.
    """
    if not password_keys:
        return field_data
    return {k: (_REDACTED_VALUE if k in password_keys and v else v) for k, v in field_data.items()}


def _fault_injection_enabled() -> bool:
    return os.environ.get("HERD_FAULT_INJECTION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class DeviceStatusUpdate(BaseModel):
    status: DeviceStatus


class ResolveByNameRequest(BaseModel):
    names: list[str]


class ResolveByNameResponse(BaseModel):
    # name to device id; names with no match are omitted so the caller can
    # report them as unresolved.
    resolved: dict[str, uuid.UUID]


class HealthConfigEntry(BaseModel):
    device_id: uuid.UUID
    resolved_interval_seconds: int


def _device_to_response(
    device: Device,
    *,
    redact_passwords: bool = False,
    password_keys: set[str] | None = None,
) -> DeviceResponse:
    template_poll = device.template.poll_interval_seconds if device.template else None
    resolved_poll = device.poll_interval_seconds or template_poll
    field_data = device.field_data
    if redact_passwords:
        if password_keys is None:
            password_keys = _password_field_keys(device.template)
        field_data = _redact_field_data(field_data, password_keys)
    return DeviceResponse(
        id=device.id,
        name=device.name,
        template_id=device.template_id,
        template_name=device.template.name if device.template else None,
        template_icon=device.template.icon if device.template else None,
        template_vendor=device.template.vendor if device.template else None,
        template_model=device.template.model if device.template else None,
        template_part_number=device.template.part_number if device.template else None,
        topology_type=device.topology_type,
        status=device.status,
        field_data=field_data,
        driver_id=device.template.driver_id if device.template else None,
        driver_name=(
            device.template.driver.name if device.template and device.template.driver else None
        ),
        driver_sha256=(
            device.template.driver.sha256 if device.template and device.template.driver else None
        ),
        driver_filename=(
            device.template.driver.filename if device.template and device.template.driver else None
        ),
        connection_type=(
            device.template.driver.connection_type
            if device.template and device.template.driver
            else None
        ),
        exclusive=device.template.exclusive if device.template else True,
        created_at=device.created_at,
        updated_at=device.updated_at,
        created_by=device.created_by,
        created_by_name=device.created_by_name,
        modified_by=device.modified_by,
        modified_by_name=device.modified_by_name,
        poll_interval_seconds=device.poll_interval_seconds,
        resolved_poll_interval_seconds=resolved_poll,
    )


@router.get("/devices", response_model=PaginatedDeviceResponse)
async def get_devices(
    template_id: uuid.UUID | None = Query(None),
    topology_type: TopologyType | None = Query(None),
    status: DeviceStatus | None = Query(None),
    dut_only: bool = Query(False),
    search: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    """List devices. Non-admin users only see DUTs visible through group assignments."""
    role = payload.get("role", "user")
    visible_ids = None
    is_admin = role in ("admin", "superadmin")
    if not is_admin:
        dut_only = True
        # Fail closed: a malformed token subject is an auth error, not a reason
        # to fall through to showing every device. _resolve_visible_device_ids
        # likewise propagates a 503 on an auth-service outage.
        try:
            user_id = uuid.UUID(payload["sub"])
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=401, detail="invalid token subject") from exc
        visible_ids = await _resolve_visible_device_ids(db, user_id, authorization)

    devices, total = await list_devices(
        db,
        template_id,
        topology_type,
        status,
        skip,
        limit,
        visible_device_ids=visible_ids,
        dut_only=dut_only,
        search=search,
    )

    if is_admin:
        items = [_device_to_response(d) for d in devices]
    else:
        # Redact password-typed field values for non-admins. Resolve each
        # template's password-key set once per template_id: the template is
        # eager-loaded on Device (lazy="joined"), so this reads already-loaded
        # data and adds no per-device query.
        pw_key_cache: dict[uuid.UUID, set[str]] = {}
        items = []
        for d in devices:
            keys = pw_key_cache.get(d.template_id)
            if keys is None:
                keys = _password_field_keys(d.template)
                pw_key_cache[d.template_id] = keys
            items.append(_device_to_response(d, redact_passwords=True, password_keys=keys))

    return PaginatedDeviceResponse(
        items=items,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post("/devices", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_new_device(
    body: DeviceCreate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Add a device to the inventory. Admin or superadmin only."""
    user_id = uuid.UUID(payload["sub"])
    username = payload.get("username", "")
    device = await create_device(db, body, created_by=user_id, created_by_name=username)
    logger.info(
        "Device created: %s",
        device.name,
        extra={"action": "device_create", "device_id": str(device.id)},
    )
    return _device_to_response(device)


@router.get("/devices/health-config", response_model=list[HealthConfigEntry])
async def list_devices_health_config(
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Resolved poll-interval per device for the health-polling scheduler.

    Returns one row per device where COALESCE(device.poll_interval_seconds,
    template.poll_interval_seconds) is not null. The scheduler in the
    execution service refreshes this list periodically. Internal token only.
    """
    if not settings.internal_api_token or x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")
    resolved = func.coalesce(Device.poll_interval_seconds, DeviceTemplate.poll_interval_seconds)
    query = (
        select(Device.id, resolved.label("interval"))
        .join(DeviceTemplate, Device.template_id == DeviceTemplate.id)
        .where(resolved.is_not(None))
    )
    rows = (await db.execute(query)).all()
    return [
        HealthConfigEntry(device_id=row.id, resolved_interval_seconds=row.interval) for row in rows
    ]


@router.post("/devices/resolve-by-name", response_model=ResolveByNameResponse)
async def resolve_devices_by_name(
    body: ResolveByNameRequest,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Resolve device names to this instance's device ids. Internal token only.

    Used by the cabling service's topology import to rewrite cross-instance
    device references (which travel as names, since UUIDs do not match across
    instances) to local device ids. Names with no match are omitted from the
    response so the caller can reject the referencing rows.
    """
    if not settings.internal_api_token or x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")
    names = list({n for n in body.names if n})
    if not names:
        return ResolveByNameResponse(resolved={})
    rows = (await db.execute(select(Device).where(Device.name.in_(names)))).unique().scalars().all()
    return ResolveByNameResponse(resolved={d.name: d.id for d in rows})


@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device_by_id(
    device_id: uuid.UUID,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    """Get a single device.

    Non-admins only see devices visible through their group assignments,
    matching the list endpoint; a device outside that set returns 404 (no
    existence leak) rather than exposing its metadata and field_data.
    """
    device = await get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    role = payload.get("role", "user")
    if role not in ("admin", "superadmin"):
        try:
            user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="Device not found") from None
        visible_ids = await _resolve_visible_device_ids(db, user_id, authorization)
        if device.id not in visible_ids:
            raise HTTPException(status_code=404, detail="Device not found")
        # Non-admins may see the device but not its secrets: mask password-typed
        # field values while keeping the keys so the response shape is stable.
        return _device_to_response(device, redact_passwords=True)
    return _device_to_response(device)


@router.get("/devices/{device_id}/internal", response_model=DeviceResponse)
async def get_device_by_id_internal(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Get a single device. Internal service-to-service endpoint, guarded by token."""
    if not settings.internal_api_token or x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")
    device = await get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


@router.put("/devices/{device_id}", response_model=DeviceResponse)
async def update_device_by_id(
    device_id: uuid.UUID,
    body: DeviceUpdate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Update a device. Admin or superadmin only."""
    user_id = uuid.UUID(payload["sub"])
    username = payload.get("username", "")
    device = await update_device(
        db, device_id, body, modified_by=user_id, modified_by_name=username
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    logger.info(
        "Device updated: %s",
        device_id,
        extra={"action": "device_update", "device_id": str(device_id)},
    )
    return _device_to_response(device)


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device_by_id(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Remove a device from the inventory. Admin or superadmin only."""
    deleted = await delete_device(db, device_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Device not found")
    logger.info(
        "Device deleted: %s",
        device_id,
        extra={"action": "device_delete", "device_id": str(device_id)},
    )


@router.post("/devices/{device_id}/status", response_model=DeviceResponse)
async def update_device_status_internal(
    device_id: uuid.UUID,
    body: DeviceStatusUpdate,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Update device status. Internal service-to-service endpoint, guarded by token."""
    if not settings.internal_api_token or x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")
    # Test-only fault-injection seam (issue #214). Double-gated: active only when
    # HERD_FAULT_INJECTION is set (dev/test compose override; never in `make prod`)
    # AND the target device's name carries the sentinel. Lets integration tests
    # drive the provisioning FAILED + device-revert path by failing one device's
    # status update on demand. Inert in production (env unset) and for normal devices.
    if _fault_injection_enabled():
        existing = await get_device(db, device_id)
        if existing is not None and _FAULT_STATUS_SENTINEL in (existing.name or ""):
            raise HTTPException(
                status_code=503,
                detail="fault injection: simulated inventory status-update failure",
            )
    device = await set_device_status(db, device_id, body.status)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


async def _resolve_visible_device_ids(
    db: AsyncSession, user_id: uuid.UUID, authorization: str | None
) -> set[uuid.UUID]:
    """Resolve the set of device IDs a non-admin user can see via their groups.

    Fails closed: if the auth service is unreachable or errors,
    `_fetch_user_group_ids` raises HTTPException(503) and that propagates so the
    request fails rather than falling back to showing every device. A user who
    legitimately belongs to no groups resolves to an empty set (sees no DUTs),
    which is distinct from an auth-service outage.
    """
    from app.routers.device_groups import _fetch_user_group_ids
    from app.services.device_group_service import get_visible_device_ids

    user_group_ids = await _fetch_user_group_ids(user_id, authorization)
    return await get_visible_device_ids(db, user_group_ids)
