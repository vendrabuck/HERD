import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.models.device import DeviceStatus, DeviceType, TopologyType
from app.schemas.device import DeviceCreate, DeviceResponse, DeviceUpdate
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


class DeviceStatusUpdate(BaseModel):
    status: DeviceStatus


@router.get("/devices", response_model=list[DeviceResponse])
async def get_devices(
    device_type: DeviceType | None = Query(None),
    topology_type: TopologyType | None = Query(None),
    status: DeviceStatus | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """List devices. Available to all authenticated users."""
    return await list_devices(db, device_type, topology_type, status, skip, limit)


@router.post("/devices", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_new_device(
    body: DeviceCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Add a device to the inventory. Admin or superadmin only."""
    device = await create_device(db, body)
    logger.info(
        "Device created: %s", device.name,
        extra={"action": "device_create", "device_id": str(device.id)},
    )
    return device


@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device_by_id(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Get a single device. Available to all authenticated users."""
    device = await get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.put("/devices/{device_id}", response_model=DeviceResponse)
async def update_device_by_id(
    device_id: uuid.UUID,
    body: DeviceUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Update a device. Admin or superadmin only."""
    device = await update_device(db, device_id, body)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    logger.info(
        "Device updated: %s", device_id,
        extra={"action": "device_update", "device_id": str(device_id)},
    )
    return device


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
        "Device deleted: %s", device_id,
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
    device = await set_device_status(db, device_id, body.status)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device
