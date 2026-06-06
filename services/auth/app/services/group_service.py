import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.group import GroupMember, UserGroup
from app.models.user import User

logger = logging.getLogger(__name__)


async def create_group(
    db: AsyncSession, name: str, description: str | None, created_by: uuid.UUID
) -> UserGroup:
    group = UserGroup(name=name, description=description, created_by=created_by)
    db.add(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(group)
    return group


async def get_all_groups(
    db: AsyncSession, skip: int = 0, limit: int = 50
) -> tuple[list[UserGroup], int]:
    from sqlalchemy import func

    count_query = select(func.count()).select_from(UserGroup)
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        select(UserGroup).order_by(UserGroup.created_at).offset(skip).limit(limit)
    )
    return list(result.scalars().all()), total


async def get_group_by_id(db: AsyncSession, group_id: uuid.UUID) -> UserGroup | None:
    result = await db.execute(
        select(UserGroup).where(UserGroup.id == group_id).options(selectinload(UserGroup.members))
    )
    return result.scalar_one_or_none()


async def update_group(
    db: AsyncSession,
    group: UserGroup,
    name: str | None,
    description: str | None,
    modified_by: uuid.UUID | None = None,
) -> UserGroup:
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
        raise
    await db.refresh(group)
    return group


async def delete_group(db: AsyncSession, group: UserGroup) -> None:
    await db.delete(group)
    await db.commit()


async def _remove_from_not_grouped(db: AsyncSession, user_ids: list[uuid.UUID]) -> None:
    """Remove users from the 'Not Grouped' default group if they are in it."""
    not_grouped = await get_group_by_name(db, "Not Grouped")
    if not not_grouped:
        return
    await db.execute(
        delete(GroupMember).where(
            GroupMember.group_id == not_grouped.id,
            GroupMember.user_id.in_(user_ids),
        )
    )


async def add_member(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> GroupMember:
    existing = await db.execute(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise IntegrityError("Duplicate membership", params=None, orig=None)
    member = GroupMember(group_id=group_id, user_id=user_id)
    db.add(member)
    # Auto-remove from "Not Grouped" when added to any other group
    not_grouped = await get_group_by_name(db, "Not Grouped")
    if not not_grouped or group_id != not_grouped.id:
        await _remove_from_not_grouped(db, [user_id])
    await db.commit()
    await db.refresh(member)
    return member


async def remove_member(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        return False
    await db.delete(member)
    await db.commit()
    return True


async def get_group_members(db: AsyncSession, group_id: uuid.UUID) -> list[dict]:
    result = await db.execute(
        select(GroupMember, User)
        .join(User, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group_id)
        .order_by(GroupMember.added_at)
    )
    rows = result.all()
    return [
        {
            "user_id": member.user_id,
            "username": user.username,
            "email": user.email,
            "added_at": member.added_at,
        }
        for member, user in rows
    ]


async def get_user_groups(db: AsyncSession, user_id: uuid.UUID) -> list[UserGroup]:
    result = await db.execute(
        select(UserGroup)
        .join(GroupMember, GroupMember.group_id == UserGroup.id)
        .where(GroupMember.user_id == user_id)
        .order_by(UserGroup.name)
    )
    return list(result.scalars().all())


async def bulk_add_members(
    db: AsyncSession, group_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> tuple[int, int]:
    added = 0
    skipped = 0
    added_ids: list[uuid.UUID] = []
    for user_id in user_ids:
        # Check existence first to avoid IntegrityError
        existing = await db.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue
        member = GroupMember(group_id=group_id, user_id=user_id)
        db.add(member)
        added_ids.append(user_id)
        added += 1
    # Auto-remove from "Not Grouped" when added to any other group
    if added_ids:
        not_grouped = await get_group_by_name(db, "Not Grouped")
        if not not_grouped or group_id != not_grouped.id:
            await _remove_from_not_grouped(db, added_ids)
    await db.commit()
    return added, skipped


async def bulk_remove_members(
    db: AsyncSession, group_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> tuple[int, int]:
    removed = 0
    not_found = 0
    for user_id in user_ids:
        result = await db.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member:
            await db.delete(member)
            removed += 1
        else:
            not_found += 1
    await db.commit()
    return removed, not_found


async def get_group_by_name(db: AsyncSession, name: str) -> UserGroup | None:
    result = await db.execute(select(UserGroup).where(UserGroup.name == name))
    return result.scalar_one_or_none()
