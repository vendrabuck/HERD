import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from herd_common.auth import ADMIN_ROLES, make_auth_dependencies
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.grant import (
    BatchCheckRequest,
    BatchCheckResponse,
    CheckRequest,
    CheckResponse,
    GrantCreateRequest,
    GrantResponse,
    PaginatedGrantResponse,
    ResourceRef,
)
from app.services.auth_client import fetch_user_groups
from app.services.grant_service import (
    batch_check,
    check_permission,
    create_grant,
    delete_grant,
    get_accessible_resources,
    get_grant,
    list_grants,
)

logger = logging.getLogger(__name__)

get_current_user_payload, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(tags=["grants"])
bearer_scheme = HTTPBearer()


def _authorize_subject(payload: dict, target_user_id: uuid.UUID) -> None:
    """Enforce that a permission query is for the caller's own identity.

    The /check, /check/batch, and /resources endpoints answer "what can this
    user access", which is self-service per docs/ROLES.md. Without this guard a
    user could pass any user_id and enumerate another user's access (IDOR).
    Admins and superadmins may introspect any user. Raises 403 otherwise.
    """
    if payload.get("role") in ADMIN_ROLES:
        return
    if str(target_user_id) != payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot query permissions for another user",
        )


@router.post("/grants", response_model=GrantResponse, status_code=status.HTTP_201_CREATED)
async def create_grant_endpoint(
    body: GrantCreateRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    granted_by = uuid.UUID(payload["sub"])
    try:
        grant = await create_grant(
            db,
            body.group_id,
            body.resource_type,
            body.resource_id,
            body.permission,
            granted_by,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This grant already exists",
        )
    logger.info(
        "Grant created: group %s, %s on %s %s",
        body.group_id,
        body.permission,
        body.resource_type,
        body.resource_id,
        extra={
            "action": "grant_create",
            "grant_id": str(grant.id),
            "group_id": str(body.group_id),
            "resource_type": body.resource_type,
            "resource_id": str(body.resource_id),
            "permission": body.permission,
        },
    )
    return grant


@router.get("/grants", response_model=PaginatedGrantResponse)
async def list_grants_endpoint(
    group_id: uuid.UUID | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    grants, total = await list_grants(
        db, group_id, resource_type, resource_id, skip=skip, limit=limit
    )
    return PaginatedGrantResponse(
        items=grants,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/grants/{grant_id}", response_model=GrantResponse)
async def get_grant_endpoint(
    grant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    grant = await get_grant(db, grant_id)
    if not grant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    return grant


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_grant_endpoint(
    grant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    grant = await get_grant(db, grant_id)
    if not grant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    await delete_grant(db, grant)
    logger.info(
        "Grant deleted: %s by %s",
        grant_id,
        payload["sub"],
        extra={
            "action": "grant_delete",
            "grant_id": str(grant_id),
        },
    )


@router.post("/check", response_model=CheckResponse)
async def check_permission_endpoint(
    body: CheckRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    _authorize_subject(payload, body.user_id)
    group_ids = await fetch_user_groups(
        settings.auth_service_url, body.user_id, credentials.credentials
    )
    allowed, grants = await check_permission(
        db, group_ids, body.resource_type, body.resource_id, body.permission
    )
    return CheckResponse(allowed=allowed, grants=grants)


@router.post("/check/batch", response_model=BatchCheckResponse)
async def batch_check_endpoint(
    body: BatchCheckRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    _authorize_subject(payload, body.user_id)
    group_ids = await fetch_user_groups(
        settings.auth_service_url, body.user_id, credentials.credentials
    )
    results = await batch_check(
        db, group_ids, body.resource_type, body.resource_ids, body.permission
    )
    return BatchCheckResponse(results=results)


@router.get("/resources", response_model=list[ResourceRef])
async def get_resources_endpoint(
    user_id: uuid.UUID = Query(),
    resource_type: str = Query(),
    permission: str = Query(),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    _authorize_subject(payload, user_id)
    group_ids = await fetch_user_groups(settings.auth_service_url, user_id, credentials.credentials)
    resource_ids = await get_accessible_resources(db, group_ids, resource_type, permission)
    return [ResourceRef(resource_type=resource_type, resource_id=rid) for rid in resource_ids]
