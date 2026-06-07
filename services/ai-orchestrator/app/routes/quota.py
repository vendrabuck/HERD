"""GET /api/ai/quota: the caller's daily AI token budget for the current UTC day.

A lightweight read endpoint so the frontend can show "X of Y tokens used today,
resets at Z" near the AI entry points and surface the over-quota reason inline.
Unlike the billable AI routes, this is not gated on ai_is_configured() (budget
state is meaningful regardless of provider configuration) and works even when
the caller is already over quota, since it is a read, not a billable call. When
no quota is configured it reports enabled=false.
"""

import uuid

from fastapi import APIRouter, Depends
from herd_common.auth import make_auth_dependencies
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import usage_repo

get_current_user, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/quota", tags=["quota"])


@router.get("")
async def get_quota(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    user_id = uuid.UUID(user["sub"])
    status = await usage_repo.get_status(db, user_id)
    return {
        "enabled": status.enabled,
        "limit": status.limit,
        "used": status.used,
        "remaining": status.remaining,
        "reset_at": status.reset_at.isoformat(),
    }
