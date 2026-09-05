import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_role
from app.models.user import Role, User
from app.schemas.auth import PaginatedUserResponse, SetRoleRequest, UserResponse
from app.services.auth_service import (
    get_all_users,
    get_user_by_id,
    set_user_active,
    set_user_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["admin"])

_superadmin_only = Depends(require_role(Role.SUPERADMIN))
_admin_or_superadmin = Depends(require_role(Role.ADMIN, Role.SUPERADMIN))


@router.get("", response_model=PaginatedUserResponse)
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = _admin_or_superadmin,
):
    """List all registered users. Admin or superadmin."""
    users, total = await get_all_users(db, skip=skip, limit=limit)
    return PaginatedUserResponse(
        items=users,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.put("/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: uuid.UUID,
    body: SetRoleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _superadmin_only,
):
    """
    Set a user's role to 'user' or 'admin'. Superadmin only.

    Rules:
    - Cannot set role to 'superadmin' (there is exactly one superadmin).
    - Cannot change the role of the superadmin account itself.
    - Cannot change your own role.
    """
    if body.role == Role.SUPERADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot assign the superadmin role via the API",
        )

    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change your own role",
        )

    target = await get_user_by_id(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target.role == Role.SUPERADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change the superadmin's role",
        )

    updated = await set_user_role(db, user_id, body.role, modified_by=current_user.id)
    logger.info(
        "Role changed: user %s set to %s by %s",
        user_id,
        body.role.value,
        current_user.username,
        extra={
            "action": "role_change",
            "user_id": str(user_id),
            "role": body.role.value,
        },
    )
    return updated


@router.post("/{user_id}/activate", response_model=UserResponse)
async def activate_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    """Manually reactivate a user (ADR 0011 phase 4). Admin or superadmin.

    Always clears deactivated_by_sync: this is the non-sync writer of
    is_active, and admin intent outranks the directory.
    """
    target = await get_user_by_id(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    updated = await set_user_active(db, user_id, True)
    logger.info(
        "User activated: %s by %s",
        user_id,
        current_user.username,
        extra={"action": "user_activate", "user_id": str(user_id)},
    )
    return updated


@router.post("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    """Manually deactivate a user (ADR 0011 phase 4). Admin or superadmin.

    Always clears deactivated_by_sync: a manually deactivated user is
    invisible to the sweep's automatic reactivation.

    The superadmin is refused (issue #715), mirroring the role-change
    carve-out: a deactivated superadmin cannot log in or refresh, is not
    re-seeded at startup, and the LDAP sync never reactivates a local user.
    activate_user deliberately has NO matching carve-out; that is the
    recovery direction.
    """
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot deactivate your own account",
        )

    target = await get_user_by_id(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target.role == Role.SUPERADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate the superadmin",
        )

    updated = await set_user_active(db, user_id, False)
    logger.info(
        "User deactivated: %s by %s",
        user_id,
        current_user.username,
        extra={"action": "user_deactivate", "user_id": str(user_id)},
    )
    return updated
