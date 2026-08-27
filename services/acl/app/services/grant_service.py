import uuid

from herd_common.pagination import paginate
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grant import ResourceGrant


async def create_grant(
    db: AsyncSession,
    group_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    permission: str,
    granted_by: uuid.UUID | None,
) -> ResourceGrant:
    grant = ResourceGrant(
        group_id=group_id,
        resource_type=resource_type,
        resource_id=resource_id,
        permission=permission,
        granted_by=granted_by,
    )
    db.add(grant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(grant)
    return grant


async def list_grants(
    db: AsyncSession,
    group_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[ResourceGrant], int]:
    stmt = select(ResourceGrant)
    if group_id is not None:
        stmt = stmt.where(ResourceGrant.group_id == group_id)
    if resource_type is not None:
        stmt = stmt.where(ResourceGrant.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(ResourceGrant.resource_id == resource_id)

    stmt = stmt.order_by(ResourceGrant.granted_at)
    return await paginate(db, stmt, skip=skip, limit=limit)


async def get_grant(db: AsyncSession, grant_id: uuid.UUID) -> ResourceGrant | None:
    result = await db.execute(select(ResourceGrant).where(ResourceGrant.id == grant_id))
    return result.scalar_one_or_none()


async def delete_grant(db: AsyncSession, grant: ResourceGrant) -> None:
    await db.delete(grant)
    await db.commit()


async def check_permission(
    db: AsyncSession,
    group_ids: list[uuid.UUID],
    resource_type: str,
    resource_id: uuid.UUID,
    permission: str,
) -> tuple[bool, list[ResourceGrant]]:
    if not group_ids:
        return False, []

    # "manage" implies "view": if checking "view", also accept "manage" grants
    permissions_to_check = [permission]
    if permission == "view":
        permissions_to_check.append("manage")

    stmt = select(ResourceGrant).where(
        ResourceGrant.group_id.in_(group_ids),
        ResourceGrant.resource_type == resource_type,
        ResourceGrant.resource_id == resource_id,
        ResourceGrant.permission.in_(permissions_to_check),
    )
    result = await db.execute(stmt)
    grants = list(result.scalars().all())
    return len(grants) > 0, grants


async def get_accessible_resources(
    db: AsyncSession,
    group_ids: list[uuid.UUID],
    resource_type: str,
    permission: str,
) -> list[uuid.UUID]:
    if not group_ids:
        return []

    permissions_to_check = [permission]
    if permission == "view":
        permissions_to_check.append("manage")

    stmt = (
        select(ResourceGrant.resource_id)
        .where(
            ResourceGrant.group_id.in_(group_ids),
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.permission.in_(permissions_to_check),
        )
        .distinct()
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def batch_check(
    db: AsyncSession,
    group_ids: list[uuid.UUID],
    resource_type: str,
    resource_ids: list[uuid.UUID],
    permission: str,
) -> dict[str, bool]:
    if not group_ids:
        return {str(rid): False for rid in resource_ids}

    permissions_to_check = [permission]
    if permission == "view":
        permissions_to_check.append("manage")

    stmt = (
        select(ResourceGrant.resource_id)
        .where(
            ResourceGrant.group_id.in_(group_ids),
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id.in_(resource_ids),
            ResourceGrant.permission.in_(permissions_to_check),
        )
        .distinct()
    )
    result = await db.execute(stmt)
    allowed_ids = {rid for rid in result.scalars().all()}
    return {str(rid): rid in allowed_ids for rid in resource_ids}
