import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceStatus, DeviceType, TopologyType
from app.schemas.device import DeviceCreate, DeviceUpdate


async def list_devices(
    db: AsyncSession,
    device_type: DeviceType | None = None,
    topology_type: TopologyType | None = None,
    status: DeviceStatus | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[Device]:
    query = select(Device)
    if device_type:
        query = query.where(Device.device_type == device_type)
    if topology_type:
        query = query.where(Device.topology_type == topology_type)
    if status:
        query = query.where(Device.status == status)
    query = query.offset(skip).limit(limit).order_by(Device.created_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_device(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    result = await db.execute(select(Device).where(Device.id == device_id))
    return result.scalar_one_or_none()


async def get_devices_by_ids(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    result = await db.execute(select(Device).where(Device.id.in_(device_ids)))
    return list(result.scalars().all())


async def create_device(db: AsyncSession, data: DeviceCreate) -> Device:
    device = Device(**data.model_dump())
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


async def update_device(
    db: AsyncSession, device_id: uuid.UUID, data: DeviceUpdate
) -> Device | None:
    device = await get_device(db, device_id)
    if not device:
        return None
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(device, field, value)
    await db.commit()
    await db.refresh(device)
    return device


async def delete_device(db: AsyncSession, device_id: uuid.UUID) -> bool:
    device = await get_device(db, device_id)
    if not device:
        return False
    await db.delete(device)
    await db.commit()
    return True


async def set_device_status(
    db: AsyncSession, device_id: uuid.UUID, status: DeviceStatus
) -> Device | None:
    device = await get_device(db, device_id)
    if not device:
        return None
    device.status = status
    await db.commit()
    await db.refresh(device)
    return device
