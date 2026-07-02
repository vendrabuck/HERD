import logging
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_token import ApiToken
from app.models.user import Role, User
from app.services.auth_service import get_user_by_id
from app.utils.jwt import create_access_token, hash_token

logger = logging.getLogger(__name__)

# Role ordering: a token may never grant more than its owning principal has.
_ROLE_RANK = {Role.USER: 0, Role.ADMIN: 1, Role.SUPERADMIN: 2}


def _role_exceeds(requested: Role, principal: Role) -> bool:
    return _ROLE_RANK[requested] > _ROLE_RANK[principal]


async def create_api_token(
    db: AsyncSession,
    *,
    name: str,
    principal: User,
    role: Role,
    expires_at: datetime | None,
    created_by: uuid.UUID | None,
) -> tuple[ApiToken, str]:
    """Create a token for a principal and return (row, raw_token).

    The raw token is generated here, hashed, and only the hash is stored. The
    raw value is returned to the caller exactly once and is never persisted or
    logged.
    """
    if _role_exceeds(role, principal.role):
        raise ValueError("Token role cannot exceed the principal's role")

    raw_token = secrets.token_urlsafe(32)
    token = ApiToken(
        name=name,
        principal_id=principal.id,
        token_hash=hash_token(raw_token),
        role=role,
        is_active=True,
        expires_at=expires_at,
        created_by=created_by,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    logger.info(
        "API token created",
        extra={
            "action": "api_token_create",
            "token_id": str(token.id),
            "principal_id": str(token.principal_id),
            "role": token.role.value,
        },
    )
    return token, raw_token


async def exchange_api_token(db: AsyncSession, raw_token: str) -> str | None:
    """Exchange a raw API token for a short-lived access JWT, or None on failure.

    Failure (unknown, revoked, expired token, or inactive principal) always
    returns None so the caller can answer with a single generic 401 that does
    not reveal which condition failed. No refresh token is issued: a machine
    re-exchanges its long-lived token when the access JWT expires.
    """
    token_hash = hash_token(raw_token)
    result = await db.execute(select(ApiToken).where(ApiToken.token_hash == token_hash))
    token = result.scalar_one_or_none()
    if token is None or not token.is_active:
        return None

    now = datetime.now(timezone.utc)
    if token.expires_at is not None:
        expires_at = token.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return None

    principal = await get_user_by_id(db, token.principal_id)
    if principal is None or not principal.is_active:
        return None

    token.last_used_at = now
    await db.commit()

    # Re-check the principal's CURRENT role at exchange time: the token snapshots
    # the role at creation, so a since-demoted principal must not keep elevated
    # access. Clamp to the least-privileged of the token's role and the live role
    # (min by rank) rather than failing outright, matching least-privilege.
    effective_role = token.role if not _role_exceeds(token.role, principal.role) else principal.role

    payload = {
        "sub": str(principal.id),
        "username": principal.username,
        "email": principal.email,
        "role": effective_role.value,
        "auth_source": "api_token",
    }
    return create_access_token(payload)


async def list_api_tokens(db: AsyncSession) -> list[ApiToken]:
    result = await db.execute(select(ApiToken).order_by(ApiToken.created_at.desc()))
    return list(result.scalars().all())


async def revoke_api_token(db: AsyncSession, token_id: uuid.UUID) -> None:
    """Deactivate a token. Idempotent: a missing or already-revoked token is a no-op."""
    await db.execute(update(ApiToken).where(ApiToken.id == token_id).values(is_active=False))
    await db.commit()
