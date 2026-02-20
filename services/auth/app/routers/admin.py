import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_role
from app.models.user import Role, User
from app.schemas.auth import SetRoleRequest, UserResponse
from app.services.auth_service import get_all_users, get_user_by_id, set_user_role

router = APIRouter(prefix="/users", tags=["admin"])

_superadmin_only = Depends(require_role(Role.SUPERADMIN))


@router.get("", response_model=list[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = _superadmin_only,
):
    """List all registered users. Superadmin only."""
    return await get_all_users(db)


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

    updated = await set_user_role(db, user_id, body.role)
    return updated
