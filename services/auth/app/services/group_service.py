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


async def _remove_from_not_grouped(
    db: AsyncSession, user_ids: list[uuid.UUID], not_grouped_id: uuid.UUID | None
) -> None:
    """Remove users from the 'Not Grouped' default group if they are in it.

    not_grouped_id is the CALLER's already-resolved id (None if the group
    does not exist): every call site resolves it itself (by name, or from a
    run-level cache), so this function never performs its own lookup.
    """
    if not_grouped_id is None:
        return
    await db.execute(
        delete(GroupMember).where(
            GroupMember.group_id == not_grouped_id,
            GroupMember.user_id.in_(user_ids),
        )
    )


async def _add_member_resolved(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    not_grouped_id: uuid.UUID | None,
) -> GroupMember:
    """Core add_member logic taking an ALREADY-RESOLVED "Not Grouped" id
    (None if the group does not exist).

    issue #513 items 4/6/9: this is the internal entry point a caller that
    has already resolved "Not Grouped" once for a whole run uses directly,
    bypassing the public add_member wrapper's own by-name lookup below.
    ldap_sync_service calls this for both its explicit membership adds and
    (via create_ldap_user's not_grouped_id passthrough to
    auth_service._auto_assign_not_grouped) its provisioning path, so both
    agree on the SAME resolved id within one run. The public add_member
    keeps its original 3-arg signature and simply resolves-then-delegates,
    so every other caller (routers, bulk_add_members, tests) is unaffected.
    """
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
    if not_grouped_id is None or group_id != not_grouped_id:
        await _remove_from_not_grouped(db, [user_id], not_grouped_id)
    await db.commit()
    await db.refresh(member)
    return member


async def add_member(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> GroupMember:
    """Add user_id to group_id, auto-removing it from "Not Grouped".

    Original 3-arg public signature (issue #513 item 9): resolves "Not
    Grouped" by name on every call, exactly as before #513. A caller
    driving many adds within one run and wanting to resolve it once should
    call _add_member_resolved directly with its own cached id.
    """
    not_grouped = await get_group_by_name(db, "Not Grouped")
    return await _add_member_resolved(
        db, group_id, user_id, not_grouped.id if not_grouped else None
    )


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


async def get_user_groups_map(
    db: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[UserGroup]]:
    """Resolve group memberships for many users in a single query.

    Returns a map of user_id to its groups (name-ordered). Every requested
    user_id is present in the result; a user with no memberships maps to an
    empty list. This is the batch form of get_user_groups, used to avoid an
    N+1 fan-out when a caller needs memberships for a whole user set.
    """
    out: dict[uuid.UUID, list[UserGroup]] = {uid: [] for uid in user_ids}
    if not user_ids:
        return out
    result = await db.execute(
        select(GroupMember.user_id, UserGroup)
        .join(UserGroup, GroupMember.group_id == UserGroup.id)
        .where(GroupMember.user_id.in_(user_ids))
        .order_by(UserGroup.name)
    )
    for user_id, group in result.all():
        out[user_id].append(group)
    return out


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
        not_grouped_id = not_grouped.id if not_grouped else None
        if not_grouped_id is None or group_id != not_grouped_id:
            await _remove_from_not_grouped(db, added_ids, not_grouped_id)
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
