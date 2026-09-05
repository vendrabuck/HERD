"""Internal-token-guarded permission check for callers with no user JWT.

Issue #704: a background job (inventory's scheduled config-apply firing)
must re-check its creator's authority at fire time, but the scheduler holds
only the job's created_by user_id, not a bearer token. POST /internal/check
runs the same grant evaluation as the JWT-guarded POST /check in grants.py,
resolving the caller's groups through auth's internal-token-guarded
GET /internal/users/{user_id}/groups instead of the forwarded-JWT
GET /groups/user/{id}, so the two routes share the evaluation
(check_permission) and differ only in how the group set is resolved.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from herd_common.internal_auth import internal_token_matches
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.grant import CheckRequest, CheckResponse
from app.services.auth_client import fetch_user_groups_internal
from app.services.grant_service import check_permission

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


@router.post("/internal/check", response_model=CheckResponse)
async def check_permission_internal(
    body: CheckRequest,
    _: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_db),
) -> CheckResponse:
    """Same shape and evaluation as POST /check, token-guarded instead of JWT-guarded.

    Closed by default: fetch_user_groups_internal returns an empty group
    list on any transport failure or non-200 response from auth, and
    check_permission already treats an empty group list as "no grant
    possible" (returns False, []) rather than raising, so an unreachable
    auth service yields allowed=False here without special-casing.
    """
    group_ids = await fetch_user_groups_internal(
        settings.auth_service_url, body.user_id, settings.internal_api_token
    )
    allowed, grants = await check_permission(
        db, group_ids, body.resource_type, body.resource_id, body.permission
    )
    return CheckResponse(allowed=allowed, grants=grants)
