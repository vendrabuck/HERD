import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.template import TemplateCreate, TemplateUpdate


async def list_templates(
    db: AsyncSession, template_type: str | None = None, skip: int = 0, limit: int = 50
) -> tuple[list[DeviceTemplate], int]:
    query = select(DeviceTemplate).order_by(DeviceTemplate.created_at.desc())
    if template_type:
        query = query.where(DeviceTemplate.template_type == template_type)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all()), total


async def get_template(db: AsyncSession, template_id: uuid.UUID) -> DeviceTemplate | None:
    result = await db.execute(select(DeviceTemplate).where(DeviceTemplate.id == template_id))
    return result.scalar_one_or_none()


async def create_template(db: AsyncSession, data: TemplateCreate) -> DeviceTemplate:
    template = DeviceTemplate(
        name=data.name,
        template_type=data.template_type,
        driver_id=data.driver_id,
        exclusive=data.exclusive,
        icon=data.icon,
        description=data.description,
        vendor=(data.vendor or "unknown"),
        model=(data.model or "unknown"),
        part_number=data.part_number,
        sections=[s.model_dump() for s in data.sections],
        poll_interval_seconds=data.poll_interval_seconds,
    )
    db.add(template)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Template with name '{data.name}' already exists",
        )
    await db.refresh(template)
    return template


async def update_template(
    db: AsyncSession,
    template_id: uuid.UUID,
    data: TemplateUpdate,
    modified_by: uuid.UUID | None = None,
) -> DeviceTemplate | None:
    template = await get_template(db, template_id)
    if not template:
        return None
    update_data = data.model_dump(exclude_unset=True)
    if modified_by is not None:
        template.modified_by = modified_by
    if "sections" in update_data and update_data["sections"] is not None:
        update_data["sections"] = [s.model_dump() for s in data.sections]
    for field, value in update_data.items():
        setattr(template, field, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        conflict_name = data.name or template.name
        raise HTTPException(
            status_code=409,
            detail=f"Template with name '{conflict_name}' already exists",
        )
    await db.refresh(template)
    return template


async def delete_template(db: AsyncSession, template_id: uuid.UUID) -> bool:
    template = await get_template(db, template_id)
    if not template:
        return False
    # Check if any devices reference this template
    count_result = await db.execute(
        select(func.count()).select_from(Device).where(Device.template_id == template_id)
    )
    device_count = count_result.scalar()
    if device_count and device_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete template: devices still reference it",
        )
    # Check if any ports reference this template
    port_count_result = await db.execute(
        select(func.count()).select_from(Port).where(Port.template_id == template_id)
    )
    port_count = port_count_result.scalar()
    if port_count and port_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete template: ports still reference it",
        )
    await db.delete(template)
    await db.commit()
    return True
