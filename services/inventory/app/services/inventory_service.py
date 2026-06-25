import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceStatus, TopologyType
from app.models.driver_package import ConnectionType, DriverPackage
from app.models.template import DeviceTemplate
from app.schemas.device import DeviceCreate, DeviceUpdate


def validate_field_data(template: DeviceTemplate, field_data: dict[str, Any]) -> None:
    """Validate field_data against the template's section/field definitions."""
    all_fields: dict[str, dict] = {}
    for section in template.sections:
        for field in section["fields"]:
            all_fields[field["key"]] = field

    # Check for unknown keys
    unknown = set(field_data.keys()) - set(all_fields.keys())
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown fields: {', '.join(sorted(unknown))}",
        )

    # Apply defaults for missing/empty fields
    for key, defn in all_fields.items():
        missing = key not in field_data or field_data[key] is None or field_data[key] == ""
        if missing and defn.get("default") is not None:
            field_data[key] = defn["default"]

    for key, defn in all_fields.items():
        value = field_data.get(key)

        # Required check
        if defn.get("required") and (value is None or value == ""):
            raise HTTPException(
                status_code=422,
                detail=f"Required field missing: {key}",
            )

        if value is None or value == "":
            continue

        # Type check
        ftype = defn["type"]
        if ftype in ("string", "password") and not isinstance(value, str):
            raise HTTPException(
                status_code=422,
                detail=f"Field '{key}' must be a string",
            )
        if ftype == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise HTTPException(
                status_code=422,
                detail=f"Field '{key}' must be a number",
            )
        if ftype == "boolean" and not isinstance(value, bool):
            raise HTTPException(
                status_code=422,
                detail=f"Field '{key}' must be a boolean",
            )
        if ftype == "dropdown":
            options = defn.get("options", [])
            if value not in options:
                raise HTTPException(
                    status_code=422,
                    detail=f"Field '{key}' must be one of: {', '.join(str(o) for o in options)}",
                )


async def list_devices(
    db: AsyncSession,
    template_id: uuid.UUID | None = None,
    topology_type: TopologyType | None = None,
    status: DeviceStatus | None = None,
    skip: int = 0,
    limit: int = 100,
    visible_device_ids: set[uuid.UUID] | None = None,
    dut_only: bool = False,
    search: str | None = None,
) -> tuple[list[Device], int]:
    query = select(Device)
    if template_id:
        query = query.where(Device.template_id == template_id)
    if topology_type:
        query = query.where(Device.topology_type == topology_type)
    if status:
        query = query.where(Device.status == status)
    if visible_device_ids is not None:
        query = query.where(Device.id.in_(visible_device_ids))
    if dut_only:
        dut_template_ids = (
            select(DeviceTemplate.id)
            .outerjoin(DriverPackage, DeviceTemplate.driver_id == DriverPackage.id)
            .where(DriverPackage.connection_type == ConnectionType.MANAGEMENT.value)
        )
        query = query.where(Device.template_id.in_(dut_template_ids))
    if search:
        query = query.where(Device.name.ilike(f"%{search}%"))

    from sqlalchemy import func

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.offset(skip).limit(limit).order_by(Device.created_at.desc())
    result = await db.execute(query)
    return list(result.unique().scalars().all()), total


async def get_device(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    result = await db.execute(select(Device).where(Device.id == device_id))
    return result.unique().scalar_one_or_none()


async def get_devices_by_ids(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    result = await db.execute(select(Device).where(Device.id.in_(device_ids)))
    return list(result.unique().scalars().all())


async def create_device(
    db: AsyncSession,
    data: DeviceCreate,
    created_by: uuid.UUID | None = None,
    created_by_name: str | None = None,
) -> Device:
    # Fetch template and validate field_data
    result = await db.execute(select(DeviceTemplate).where(DeviceTemplate.id == data.template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=422, detail="Template not found")
    if template.template_type != "device":
        raise HTTPException(status_code=422, detail="Template is not a device template")
    if template.vendor == "unknown" or template.model == "unknown":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Template '{template.name}' has unknown hardware identity. "
                "An admin must set vendor and model on this template before "
                "more devices can be created from it."
            ),
        )
    validate_field_data(template, data.field_data)

    device = Device(**data.model_dump(), created_by=created_by, created_by_name=created_by_name)
    db.add(device)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Device with name '{data.name}' already exists",
        )
    await db.refresh(device)

    # Auto-assign to "No Pool" default device group
    from app.services.device_group_service import add_device_to_no_pool

    await add_device_to_no_pool(db, device.id)

    return device


async def update_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    data: DeviceUpdate,
    modified_by: uuid.UUID | None = None,
    modified_by_name: str | None = None,
) -> Device | None:
    device = await get_device(db, device_id)
    if not device:
        return None
    update_data = data.model_dump(exclude_unset=True)
    if modified_by is not None:
        device.modified_by = modified_by
        device.modified_by_name = modified_by_name

    # Validate field_data if provided
    if "field_data" in update_data and update_data["field_data"] is not None:
        result = await db.execute(
            select(DeviceTemplate).where(DeviceTemplate.id == device.template_id)
        )
        template = result.scalar_one_or_none()
        if template:
            validate_field_data(template, update_data["field_data"])

    device_name = update_data.get("name", device.name)
    for field, value in update_data.items():
        setattr(device, field, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Device with name '{device_name}' already exists",
        )
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
