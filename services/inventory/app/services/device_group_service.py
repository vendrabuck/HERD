import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.device_group import DeviceGroup, DeviceGroupDevice, DeviceGroupPermission

logger = logging.getLogger(__name__)


async def create_device_group(
    db: AsyncSession, name: str, description: str | None, created_by: uuid.UUID | None = None
) -> DeviceGroup:
    group = DeviceGroup(name=name, description=description, created_by=created_by)
    db.add(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Device group '{name}' already exists")
    await db.refresh(group)
    return group


async def list_device_groups(
    db: AsyncSession, skip: int = 0, limit: int = 50
) -> tuple[list[DeviceGroup], int]:
    base_query = select(DeviceGroup)

    from sqlalchemy import func

    count_query = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        base_query.options(selectinload(DeviceGroup.devices), selectinload(DeviceGroup.permissions))
        .order_by(DeviceGroup.name)
        .offset(skip)
        .limit(limit)
    )
    return list(result.unique().scalars().all()), total


async def get_device_group(db: AsyncSession, group_id: uuid.UUID) -> DeviceGroup | None:
    result = await db.execute(
        select(DeviceGroup)
        .where(DeviceGroup.id == group_id)
        .options(selectinload(DeviceGroup.devices), selectinload(DeviceGroup.permissions))
    )
    return result.unique().scalar_one_or_none()


async def update_device_group(
    db: AsyncSession,
    group: DeviceGroup,
    name: str | None,
    description: str | None,
    modified_by: uuid.UUID | None = None,
) -> DeviceGroup:
    if name is not None:
        group.name = name
    if description is not None:
        group.description = description
    if modified_by is not None:
        group.modified_by = modified_by
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Device group '{name}' already exists")
    await db.refresh(group)
    return group


async def delete_device_group(db: AsyncSession, group: DeviceGroup) -> None:
    await db.delete(group)
    await db.commit()


async def _remove_from_no_pool(db: AsyncSession, device_ids: list[uuid.UUID]) -> None:
    """Remove devices from the 'No Pool' default group if they are in it."""
    no_pool = await get_no_pool_group(db)
    if not no_pool:
        return
    await db.execute(
        delete(DeviceGroupDevice).where(
            DeviceGroupDevice.device_group_id == no_pool.id,
            DeviceGroupDevice.device_id.in_(device_ids),
        )
    )


async def bulk_add_devices(
    db: AsyncSession, group_id: uuid.UUID, device_ids: list[uuid.UUID]
) -> tuple[int, int]:
    added = 0
    skipped = 0
    added_ids: list[uuid.UUID] = []
    for device_id in device_ids:
        existing = await db.execute(
            select(DeviceGroupDevice).where(
                DeviceGroupDevice.device_group_id == group_id,
                DeviceGroupDevice.device_id == device_id,
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue
        entry = DeviceGroupDevice(device_group_id=group_id, device_id=device_id)
        db.add(entry)
        added_ids.append(device_id)
        added += 1
    # Auto-remove from "No Pool" when added to any other group
    if added_ids:
        no_pool = await get_no_pool_group(db)
        if not no_pool or group_id != no_pool.id:
            await _remove_from_no_pool(db, added_ids)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request added one or more of the same (group, device)
        # rows between our existence check and this commit, tripping the unique
        # constraint. Roll back and recount what is actually present so the
        # returned (added, skipped) reflects reality instead of raising a 500.
        await db.rollback()
        present = await db.execute(
            select(DeviceGroupDevice.device_id).where(
                DeviceGroupDevice.device_group_id == group_id,
                DeviceGroupDevice.device_id.in_(device_ids),
            )
        )
        present_ids = {row[0] for row in present.all()}
        added = len(present_ids)
        skipped = len(set(device_ids)) - added
    return added, skipped


async def bulk_remove_devices(
    db: AsyncSession, group_id: uuid.UUID, device_ids: list[uuid.UUID]
) -> tuple[int, int]:
    removed = 0
    not_found = 0
    for device_id in device_ids:
        result = await db.execute(
            select(DeviceGroupDevice).where(
                DeviceGroupDevice.device_group_id == group_id,
                DeviceGroupDevice.device_id == device_id,
            )
        )
        entry = result.scalar_one_or_none()
        if entry:
            await db.delete(entry)
            removed += 1
        else:
            not_found += 1
    await db.commit()
    return removed, not_found


async def bulk_add_user_groups(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_group_ids: list[uuid.UUID],
    assigned_by: uuid.UUID | None = None,
) -> tuple[int, int]:
    added = 0
    skipped = 0
    for ug_id in user_group_ids:
        existing = await db.execute(
            select(DeviceGroupPermission).where(
                DeviceGroupPermission.device_group_id == group_id,
                DeviceGroupPermission.user_group_id == ug_id,
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue
        entry = DeviceGroupPermission(
            device_group_id=group_id, user_group_id=ug_id, assigned_by=assigned_by
        )
        db.add(entry)
        added += 1
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent duplicate insert tripped the unique constraint between our
        # existence check and this commit. Roll back and recount the actually-
        # present rows so (added, skipped) stays correct instead of raising 500.
        await db.rollback()
        present = await db.execute(
            select(DeviceGroupPermission.user_group_id).where(
                DeviceGroupPermission.device_group_id == group_id,
                DeviceGroupPermission.user_group_id.in_(user_group_ids),
            )
        )
        present_ids = {row[0] for row in present.all()}
        added = len(present_ids)
        skipped = len(set(user_group_ids)) - added
    return added, skipped


async def bulk_remove_user_groups(
    db: AsyncSession, group_id: uuid.UUID, user_group_ids: list[uuid.UUID]
) -> tuple[int, int]:
    removed = 0
    not_found = 0
    for ug_id in user_group_ids:
        result = await db.execute(
            select(DeviceGroupPermission).where(
                DeviceGroupPermission.device_group_id == group_id,
                DeviceGroupPermission.user_group_id == ug_id,
            )
        )
        entry = result.scalar_one_or_none()
        if entry:
            await db.delete(entry)
            removed += 1
        else:
            not_found += 1
    await db.commit()
    return removed, not_found


async def get_visible_device_ids(
    db: AsyncSession, user_group_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Return the set of device IDs accessible to the given user groups."""
    if not user_group_ids:
        return set()
    result = await db.execute(
        select(DeviceGroupDevice.device_id)
        .join(
            DeviceGroupPermission,
            DeviceGroupPermission.device_group_id == DeviceGroupDevice.device_group_id,
        )
        .where(DeviceGroupPermission.user_group_id.in_(user_group_ids))
    )
    return {row[0] for row in result.all()}


async def get_device_groups_for_device(db: AsyncSession, device_id: uuid.UUID) -> list[DeviceGroup]:
    """Return all device groups that contain the given device, with permissions loaded."""
    result = await db.execute(
        select(DeviceGroup)
        .join(DeviceGroupDevice, DeviceGroupDevice.device_group_id == DeviceGroup.id)
        .where(DeviceGroupDevice.device_id == device_id)
        .options(selectinload(DeviceGroup.permissions))
        .order_by(DeviceGroup.name)
    )
    return list(result.unique().scalars().all())


async def get_no_pool_group(db: AsyncSession) -> DeviceGroup | None:
    result = await db.execute(select(DeviceGroup).where(DeviceGroup.name == "No Pool"))
    return result.scalar_one_or_none()


async def add_device_to_no_pool(db: AsyncSession, device_id: uuid.UUID) -> None:
    """Add a device to the 'No Pool' default group if it exists."""
    no_pool = await get_no_pool_group(db)
    if not no_pool:
        return
    entry = DeviceGroupDevice(device_group_id=no_pool.id, device_id=device_id)
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
