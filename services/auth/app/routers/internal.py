"""Internal-token-guarded endpoints for service-to-service calls.

This is the first internal-token endpoint on the auth service. Other
services (notifications, etc.) call here when they need to enumerate
users by role without holding a real admin JWT. Token-only auth, no
user context.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import Role, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


def _require_internal_token(x_internal_token: str = Header(...)) -> None:
    if not settings.internal_api_token:
        raise HTTPException(status_code=503, detail="Internal API token not configured")
    if x_internal_token != settings.internal_api_token:
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
