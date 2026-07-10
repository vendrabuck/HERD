import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.models.port import Port
from app.routers.devices import (
    _password_field_keys,
    _redact_field_data,
    _resolve_visible_device_ids,
)
from app.schemas.port import BulkPortCreate, PortCreate, PortResponse, PortUpdate
from app.services.port_service import (
    create_port,
    create_ports_bulk,
    delete_port,
    get_port,
    list_ports,
    update_port,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ports"])


def _port_to_response(port: Port, *, redact_passwords: bool = False) -> PortResponse:
    # A port's field_data can hold password-typed fields (its template is a
    # DeviceTemplate), so non-admin reads mask those values, reusing the device
    # router's redaction so ports and devices stay consistent (issue #310).
    field_data = port.field_data
    if redact_passwords:
        field_data = _redact_field_data(field_data or {}, _password_field_keys(port.template))
    return PortResponse(
        id=port.id,
        name=port.name,
        device_id=port.device_id,
        template_id=port.template_id,
        template_name=port.template.name if port.template else None,
        template_icon=port.template.icon if port.template else None,
        exclusive=port.template.exclusive if port.template else True,
        field_data=field_data,
        created_at=port.created_at,
        updated_at=port.updated_at,
    )


@router.get("/devices/{device_id}/ports", response_model=list[PortResponse])
async def get_device_ports(
    device_id: uuid.UUID,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    """List ports for a device.

    Non-admins only see ports of devices visible through their group
    assignments, and never the values of password-typed port fields; a device
    outside that set returns 404 (no existence leak), matching the device read
    endpoints (issue #310).
    """
    role = payload.get("role", "user")
    if role not in ("admin", "superadmin"):
        try:
            user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="Device not found") from None
        visible_ids = await _resolve_visible_device_ids(db, user_id, authorization)
        if device_id not in visible_ids:
            raise HTTPException(status_code=404, detail="Device not found")
        ports = await list_ports(db, device_id)
        return [_port_to_response(p, redact_passwords=True) for p in ports]
    ports = await list_ports(db, device_id)
    return [_port_to_response(p) for p in ports]


@router.post(
    "/devices/{device_id}/ports",
    response_model=PortResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_device_port(
    device_id: uuid.UUID,
    body: PortCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Add a port to a device. Admin or superadmin only."""
    port = await create_port(db, device_id, body)
    logger.info(
        "Port created: %s on device %s",
        port.name,
        device_id,
        extra={"action": "port_create", "port_id": str(port.id)},
    )
    return _port_to_response(port)


@router.post(
    "/devices/{device_id}/ports/bulk",
    response_model=list[PortResponse],
    status_code=status.HTTP_201_CREATED,
)
async def bulk_create_device_ports(
    device_id: uuid.UUID,
    body: BulkPortCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Bulk-create ports on a device. Admin or superadmin only."""
    ports = await create_ports_bulk(db, device_id, body)
    logger.info(
        "Bulk ports created on device %s: %d ports",
        device_id,
        len(ports),
        extra={"action": "port_bulk_create", "count": len(ports)},
    )
    return [_port_to_response(p) for p in ports]


@router.get("/ports/{port_id}", response_model=PortResponse)
async def get_port_by_id(
    port_id: uuid.UUID,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    """Get a single port.

    Non-admins only see ports of devices visible through their groups, with
    password-typed field values masked; a port on a non-visible (or absent)
    device returns 404, no existence leak (issue #310).
    """
    port = await get_port(db, port_id)
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")
    role = payload.get("role", "user")
    if role not in ("admin", "superadmin"):
        try:
            user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="Port not found") from None
        visible_ids = await _resolve_visible_device_ids(db, user_id, authorization)
        if port.device_id not in visible_ids:
            raise HTTPException(status_code=404, detail="Port not found")
        return _port_to_response(port, redact_passwords=True)
    return _port_to_response(port)


@router.put("/ports/{port_id}", response_model=PortResponse)
async def update_port_by_id(
    port_id: uuid.UUID,
    body: PortUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Update a port. Admin or superadmin only."""
    port = await update_port(db, port_id, body)
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")
    logger.info(
        "Port updated: %s",
        port_id,
        extra={"action": "port_update", "port_id": str(port_id)},
    )
    return _port_to_response(port)


@router.delete("/ports/{port_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_port_by_id(
    port_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Delete a port. Admin or superadmin only."""
    deleted = await delete_port(db, port_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Port not found")
    logger.info(
        "Port deleted: %s",
        port_id,
        extra={"action": "port_delete", "port_id": str(port_id)},
    )
