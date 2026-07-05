import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.driver_package import ConnectionType, DriverPackage
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.template import TemplateCreate, TemplateUpdate


async def _validate_driver_connection_type(
    db: AsyncSession, template_type: str, driver_id: uuid.UUID | None
) -> None:
    """Enforce the connection-type contract between a template and its driver.

    A dynamic template's driver is a recipe and must be a Hypervisor package; a
    device template's driver must not be (a device driver configures hardware,
    not a hypervisor). Port templates have no driver and are unaffected. A
    missing driver row is left to the caller's own not-found handling.
    """
    if driver_id is None:
        return
    if template_type not in ("device", "dynamic"):
        return
    result = await db.execute(select(DriverPackage).where(DriverPackage.id == driver_id))
    driver = result.scalar_one_or_none()
    if driver is None:
        return
    is_hypervisor = driver.connection_type == ConnectionType.HYPERVISOR.value
    if template_type == "dynamic" and not is_hypervisor:
        raise HTTPException(
            status_code=422,
            detail="Dynamic templates require a Hypervisor-type driver",
        )
    if template_type == "device" and is_hypervisor:
        raise HTTPException(
            status_code=422,
            detail="Device templates cannot use a Hypervisor-type driver",
        )


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
    await _validate_driver_connection_type(db, data.template_type, data.driver_id)
    template = DeviceTemplate(
        name=data.name,
        template_type=data.template_type,
        driver_id=data.driver_id,
        hypervisor_id=data.hypervisor_id,
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
    # template_type is immutable after create, so re-check the driver against the
    # existing type whenever driver_id is being changed.
    if "driver_id" in update_data:
        await _validate_driver_connection_type(db, template.template_type, update_data["driver_id"])
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
