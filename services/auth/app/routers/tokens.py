import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import require_role
from app.models.user import Role, User
from app.schemas.api_token import (
    ApiTokenCreatedResponse,
    ApiTokenResponse,
    CreateApiTokenRequest,
    ExchangeTokenRequest,
    ExchangeTokenResponse,
)
from app.services.api_token_service import (
    _role_exceeds,
    create_api_token,
    exchange_api_token,
    list_api_tokens,
    revoke_api_token,
)
from app.services.auth_service import get_user_by_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tokens", tags=["api-tokens"])

_admin_or_superadmin = Depends(require_role(Role.ADMIN, Role.SUPERADMIN))


@router.post("", response_model=ApiTokenCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_token(
    body: CreateApiTokenRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = _admin_or_superadmin,
):
    """Create an API token for a machine principal. Admin or superadmin.

    The response carries the raw token in the `token` field. It is shown exactly
    once here and can never be retrieved again.
    """
    principal = await get_user_by_id(db, body.principal_id)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Principal user not found"
        )

    # Authorization (issue #312): a caller must not mint a token that outranks
    # itself. Guard both axes: the requested role, and the chosen principal's
    # role. The principal axis is the escalation vector: without it an admin
    # selects the superadmin as principal (the service's role-vs-principal check
    # passes, superadmin does not exceed superadmin) and exchanges the token
    # into a superadmin JWT. An equal-rank principal is allowed (an admin may
    # mint an admin-role token for another admin machine account); only a
    # strictly higher rank is refused. The service still enforces the
    # token-cannot-exceed-its-principal invariant below.
    if _role_exceeds(body.role, current_user.role) or _role_exceeds(
        principal.role, current_user.role
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot mint a token whose role or principal exceeds your own role",
        )

    try:
        token, raw_token = await create_api_token(
            db,
            name=body.name,
            principal=principal,
            role=body.role,
            expires_at=body.expires_at,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return ApiTokenCreatedResponse(
        id=token.id,
        name=token.name,
        principal_id=token.principal_id,
        role=token.role,
        expires_at=token.expires_at,
        token=raw_token,
    )


@router.get("", response_model=list[ApiTokenResponse])
async def list_tokens(
    db: AsyncSession = Depends(get_db),
    _: User = _admin_or_superadmin,
):
    """List API tokens (metadata only, never the hash or raw value). Admin or superadmin."""
    return await list_api_tokens(db)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    token_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = _admin_or_superadmin,
):
    """Revoke an API token. Admin or superadmin. Idempotent."""
    await revoke_api_token(db, token_id)


@router.post("/exchange", response_model=ExchangeTokenResponse)
async def exchange_token(
    body: ExchangeTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a raw API token for a short-lived access JWT. Public, no auth required.

    This is the machine login path. Any failure (unknown, revoked, expired
    token, or inactive principal) returns the same generic 401 so the response
    does not reveal which condition failed.
    """
    access_token = await exchange_api_token(db, body.token)
    if access_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ExchangeTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
    )
