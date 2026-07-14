"""Internal-token-guarded endpoints for service-to-service calls.

This is the first internal-token endpoint on the auth service. Other
services (notifications, etc.) call here when they need to enumerate
users by role without holding a real admin JWT. Token-only auth, no
user context.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from herd_common.internal_auth import internal_token_matches
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import Role, User


class UserContact(BaseModel):
    """Minimal contact info for outbound notification channels.

    Returned by the notifications service to address email and chat
    messages. Only the fields an outbound dispatcher needs; no secrets.
    """

    user_id: uuid.UUID
    email: str
    username: str


logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


def _require_internal_token(x_internal_token: str = Header(...)) -> None:
    if not settings.internal_api_token:
        raise HTTPException(status_code=503, detail="Internal API token not configured")
    # Constant-time comparison via herd_common.internal_auth: a plain `!=`
    # short-circuits on the first differing byte, leaking length and prefix
    # timing that let an attacker recover the shared secret byte by byte.
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")


@router.get("/internal/admins", response_model=list[uuid.UUID])
async def list_admin_user_ids(
    _: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_db),
) -> list[uuid.UUID]:
    """Return the user IDs of every admin and superadmin.

    Used by notifications to fan out admin-targeted events (e.g. device
    health transitions) without holding a real admin JWT. Inactive users
    are excluded so disabled accounts do not receive alerts.
    """
    result = await db.execute(
        select(User.id).where(
            User.role.in_([Role.ADMIN, Role.SUPERADMIN]),
            User.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


@router.get("/internal/users/{user_id}/contact", response_model=UserContact)
async def get_user_contact(
    user_id: uuid.UUID,
    _: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_db),
) -> UserContact:
    """Return contact info (email, username) for a single user.

    Used by the notifications service to address outbound email and chat
    messages without holding a real JWT. Inactive users are still
    resolvable, the dispatch decision is made by the caller. 404 when the
    user does not exist so the caller can skip the channel cleanly.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserContact(user_id=user.id, email=user.email, username=user.username)
