import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user, require_role
from app.models.user import Role, User
from app.schemas.group import (
    AddMemberRequest,
    BulkAddMembersRequest,
    BulkMemberAddResponse,
    BulkMemberRemoveResponse,
    BulkRemoveMembersRequest,
    GroupCreateRequest,
    GroupDetailResponse,
    GroupMemberResponse,
    GroupResponse,
    GroupUpdateRequest,
    PaginatedGroupResponse,
)
from app.services.auth_service import get_user_by_id
from app.services.group_service import (
    add_member,
    bulk_add_members,
    bulk_remove_members,
    create_group,
    delete_group,
    get_all_groups,
    get_group_by_id,
    get_group_members,
    remove_member,
    update_group,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/groups", tags=["groups"])

_admin_or_superadmin = Depends(require_role(Role.ADMIN, Role.SUPERADMIN))


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group_endpoint(
    body: GroupCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    try:
        group = await create_group(db, body.name, body.description, current_user.id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A group with this name already exists",
        )
    logger.info(
        "Group created: %s by %s",
        group.name,
        current_user.username,
        extra={"action": "group_create", "group_id": str(group.id), "group_name": group.name},
    )
    return group


@router.get("", response_model=PaginatedGroupResponse)
async def list_groups(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    groups, total = await get_all_groups(db, skip=skip, limit=limit)
    return PaginatedGroupResponse(
        items=groups,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{group_id}", response_model=GroupDetailResponse)
async def get_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    members = await get_group_members(db, group_id)
    return GroupDetailResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_by=group.created_by,
        created_at=group.created_at,
        updated_at=group.updated_at,
        modified_by=group.modified_by,
        members=members,
    )


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group_endpoint(
    group_id: uuid.UUID,
    body: GroupUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    try:
        updated = await update_group(
            db, group, body.name, body.description, modified_by=current_user.id
        )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A group with this name already exists",
        )
    logger.info(
        "Group updated: %s by %s",
        updated.name,
        current_user.username,
        extra={"action": "group_update", "group_id": str(group_id)},
    )
    return updated


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_endpoint(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    await delete_group(db, group)
    logger.info(
        "Group deleted: %s by %s",
        group.name,
        current_user.username,
        extra={"action": "group_delete", "group_id": str(group_id)},
    )


@router.post(
    "/{group_id}/members",
    response_model=GroupMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member_endpoint(
    group_id: uuid.UUID,
    body: AddMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    user = await get_user_by_id(db, body.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        member = await add_member(db, group_id, body.user_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this group",
        )
    logger.info(
        "Member added: user %s to group %s by %s",
        body.user_id,
        group.name,
        current_user.username,
        extra={
            "action": "group_member_add",
            "group_id": str(group_id),
            "user_id": str(body.user_id),
        },
    )
    return GroupMemberResponse(
        user_id=user.id,
        username=user.username,
        email=user.email,
        added_at=member.added_at,
    )


@router.post("/{group_id}/members/bulk", response_model=BulkMemberAddResponse)
async def bulk_add_members_endpoint(
    group_id: uuid.UUID,
    body: BulkAddMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    added, skipped = await bulk_add_members(db, group_id, body.user_ids)
    logger.info(
        "Bulk members added to group %s by %s: %d added, %d skipped",
        group.name,
        current_user.username,
        added,
        skipped,
        extra={
            "action": "group_bulk_member_add",
            "group_id": str(group_id),
            "added": added,
            "skipped": skipped,
        },
    )
    return BulkMemberAddResponse(added=added, skipped=skipped)


@router.post("/{group_id}/members/bulk-remove", response_model=BulkMemberRemoveResponse)
async def bulk_remove_members_endpoint(
    group_id: uuid.UUID,
    body: BulkRemoveMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    removed, not_found = await bulk_remove_members(db, group_id, body.user_ids)
    logger.info(
        "Bulk members removed from group %s by %s: %d removed, %d not found",
        group.name,
        current_user.username,
        removed,
        not_found,
        extra={
            "action": "group_bulk_member_remove",
            "group_id": str(group_id),
            "removed": removed,
            "not_found": not_found,
        },
    )
    return BulkMemberRemoveResponse(removed=removed, not_found=not_found)


@router.get("/user/{user_id}", response_model=list[GroupResponse])
async def get_user_groups_endpoint(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.services.group_service import get_user_groups

    return await get_user_groups(db, user_id)


@router.delete(
    "/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member_endpoint(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    group = await get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    removed = await remove_member(db, group_id, user_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    logger.info(
        "Member removed: user %s from group %s by %s",
        user_id,
        group.name,
        current_user.username,
        extra={
            "action": "group_member_remove",
            "group_id": str(group_id),
            "user_id": str(user_id),
        },
    )
