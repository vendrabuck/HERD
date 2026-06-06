import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.port import BulkPortCreate, PortCreate, PortUpdate
from app.services.inventory_service import validate_field_data


async def list_ports(db: AsyncSession, device_id: uuid.UUID) -> list[Port]:
    result = await db.execute(
        select(Port).where(Port.device_id == device_id).order_by(Port.created_at)
    )
    return list(result.unique().scalars().all())


async def get_port(db: AsyncSession, port_id: uuid.UUID) -> Port | None:
    result = await db.execute(select(Port).where(Port.id == port_id))
    return result.unique().scalar_one_or_none()


async def create_port(db: AsyncSession, device_id: uuid.UUID, data: PortCreate) -> Port:
    # Verify device exists
    device_result = await db.execute(select(Device).where(Device.id == device_id))
    device = device_result.unique().scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Verify template exists and is a port template
    template_result = await db.execute(
        select(DeviceTemplate).where(DeviceTemplate.id == data.template_id)
    )
    template = template_result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=422, detail="Template not found")
    if template.template_type != "port":
        raise HTTPException(status_code=422, detail="Template is not a port template")

    validate_field_data(template, data.field_data)

    port = Port(
        name=data.name,
        device_id=device_id,
        template_id=data.template_id,
        field_data=data.field_data,
    )
    db.add(port)
    await db.commit()
    await db.refresh(port)
    return port


async def update_port(db: AsyncSession, port_id: uuid.UUID, data: PortUpdate) -> Port | None:
    port = await get_port(db, port_id)
    if not port:
        return None

    update_data = data.model_dump(exclude_unset=True)

    if "field_data" in update_data and update_data["field_data"] is not None:
        template_result = await db.execute(
            select(DeviceTemplate).where(DeviceTemplate.id == port.template_id)
        )
        template = template_result.scalar_one_or_none()
        if template:
            validate_field_data(template, update_data["field_data"])

    for field, value in update_data.items():
        setattr(port, field, value)
    await db.commit()
    await db.refresh(port)
    return port


async def create_ports_bulk(
    db: AsyncSession, device_id: uuid.UUID, data: BulkPortCreate
) -> list[Port]:
    # Verify device exists
    device_result = await db.execute(select(Device).where(Device.id == device_id))
    device = device_result.unique().scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Verify template exists and is a port template
    template_result = await db.execute(
        select(DeviceTemplate).where(DeviceTemplate.id == data.template_id)
    )
    template = template_result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=422, detail="Template not found")
    if template.template_type != "port":
        raise HTTPException(status_code=422, detail="Template is not a port template")

    validate_field_data(template, data.field_data)

    ports = [
        Port(
            name=f"{data.name_prefix}{data.starting_index + i}",
            device_id=device_id,
            template_id=data.template_id,
            field_data=data.field_data,
        )
        for i in range(data.instances)
    ]
    db.add_all(ports)
    await db.commit()
    for port in ports:
        await db.refresh(port)
    return ports


async def delete_port(db: AsyncSession, port_id: uuid.UUID) -> bool:
    port = await get_port(db, port_id)
    if not port:
        return False
    await db.delete(port)
    await db.commit()
    return True
