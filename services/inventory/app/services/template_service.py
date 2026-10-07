import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.driver_package import ConnectionType, DriverPackage
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.template import TemplateCreate, TemplateUpdate


def _integrity_kind(exc: IntegrityError) -> str:
    """Classify an IntegrityError, portably across asyncpg and aiosqlite.

    Production runs asyncpg (Postgres), which exposes a SQLSTATE on
    ``exc.orig`` (23505 unique_violation, 23503 foreign_key_violation). Unit
    tests run aiosqlite (SQLite), which carries no SQLSTATE, only message text
    ("UNIQUE constraint failed: ..." or "FOREIGN KEY constraint failed").
    Returns "unique", "foreign_key", or "unknown"; the caller maps each to a
    distinct HTTP response so a foreign-key failure is never reported as a
    name conflict.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == "23505":
        return "unique"
    if sqlstate == "23503":
        return "foreign_key"
    text = str(getattr(exc, "orig", exc)).lower()
    if "unique constraint failed" in text:
        return "unique"
    if "foreign key constraint failed" in text:
        return "foreign_key"
    return "unknown"


def _integrity_http_error(exc: IntegrityError, name: str) -> HTTPException:
    """Map a template-write IntegrityError to an accurate HTTPException.

    A unique-name collision keeps the historical 409 wording. A foreign-key
    failure (a referenced driver or hypervisor row absent at insert time)
    returns 422, matching the phase 1 validation layer's 422 for a referenced
    row that does not exist. Anything unclassified gets a safe generic 409 that
    does not claim a name conflict.
    """
    kind = _integrity_kind(exc)
    if kind == "foreign_key":
        return HTTPException(
            status_code=422,
            detail="Referenced hypervisor or driver does not exist",
        )
    if kind == "unique":
        return HTTPException(
            status_code=409,
            detail=f"Template with name '{name}' already exists",
        )
    return HTTPException(
        status_code=409,
        detail="Template violates a database constraint",
    )


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


def _validate_driver_and_hypervisor_presence(
    template_type: str, driver_id: uuid.UUID | None, hypervisor_id: uuid.UUID | None
) -> None:
    """The create-time presence rules of TemplateCreate.validate_sections, as a
    422 with the same words, for a template update's merged driver and
    hypervisor (issue #1018)."""
    message = None
    if hypervisor_id is not None and template_type != "dynamic":
        message = "hypervisor_id is only valid on dynamic templates"
    elif driver_id is not None and template_type not in ("device", "dynamic"):
        message = "driver_id is only valid on device or dynamic templates"
    elif template_type == "device" and driver_id is None:
        message = "Device templates must have a driver"
    elif template_type == "dynamic" and driver_id is None:
        message = "Dynamic templates must have a driver"
    elif template_type == "dynamic" and hypervisor_id is None:
        message = "Dynamic templates must have a hypervisor"
    if message is not None:
        raise HTTPException(status_code=422, detail=message)


async def list_templates(
    db: AsyncSession,
    template_type: str | None = None,
    skip: int = 0,
    limit: int = 50,
    visible_dynamic_hypervisor_ids: set[uuid.UUID] | None = None,
) -> tuple[list[DeviceTemplate], int]:
    """List templates, newest first.

    `visible_dynamic_hypervisor_ids` is the dynamic-template gate (issue #1053):
    None lists every template (an admin); a set keeps every non-dynamic template
    and only the dynamic templates whose hypervisor is in the set, in SQL, so
    `total` counts what the caller may see.
    """
    query = select(DeviceTemplate).order_by(DeviceTemplate.created_at.desc())
    if template_type:
        query = query.where(DeviceTemplate.template_type == template_type)
    if visible_dynamic_hypervisor_ids is not None:
        query = query.where(
            or_(
                DeviceTemplate.template_type != "dynamic",
                DeviceTemplate.hypervisor_id.in_(list(visible_dynamic_hypervisor_ids)),
            )
        )

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
    except IntegrityError as exc:
        await db.rollback()
        raise _integrity_http_error(exc, data.name)
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
    # template_type is immutable after create. Whenever the driver or the
    # hypervisor changes, re-run the create-time rules (TemplateCreate's
    # validate_sections plus the connection-type rule) on the MERGED result, so
    # an update cannot clear a device template's driver or a dynamic template's
    # hypervisor, or attach a hypervisor to a non-dynamic template (issue #1018).
    if "driver_id" in update_data or "hypervisor_id" in update_data:
        driver_id = update_data.get("driver_id", template.driver_id)
        hypervisor_id = update_data.get("hypervisor_id", template.hypervisor_id)
        _validate_driver_and_hypervisor_presence(template.template_type, driver_id, hypervisor_id)
        await _validate_driver_connection_type(db, template.template_type, driver_id)
    if modified_by is not None:
        template.modified_by = modified_by
    if "sections" in update_data and update_data["sections"] is not None:
        update_data["sections"] = [s.model_dump() for s in data.sections]
    for field, value in update_data.items():
        setattr(template, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        conflict_name = data.name or template.name
        raise _integrity_http_error(exc, conflict_name)
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
