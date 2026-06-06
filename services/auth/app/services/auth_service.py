import logging
import uuid
from datetime import datetime, timezone

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import RefreshToken, Role, User
from app.services import ldap_service
from app.utils.jwt import create_access_token, create_refresh_token, hash_token

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Pre-computed hash used when the user is not found, so that bcrypt always runs
# and login timing does not reveal whether an email exists.
_DUMMY_HASH = pwd_context.hash("not-a-real-password")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID | str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_all_users(db: AsyncSession, skip: int = 0, limit: int = 50) -> tuple[list[User], int]:
    from sqlalchemy import func

    count_query = select(func.count()).select_from(User)
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(select(User).order_by(User.created_at).offset(skip).limit(limit))
    return list(result.scalars().all()), total


async def superadmin_exists(db: AsyncSession) -> bool:
    result = await db.execute(select(User).where(User.role == Role.SUPERADMIN))
    return result.scalar_one_or_none() is not None


async def _auto_assign_not_grouped(db: AsyncSession, user: User) -> None:
    try:
        from app.services.group_service import add_member, get_group_by_name

        not_grouped = await get_group_by_name(db, "Not Grouped")
        if not_grouped:
            await add_member(db, not_grouped.id, user.id)
    except Exception:
        logger.debug("Could not auto-assign user %s to 'Not Grouped'", user.username)


async def create_user(
    db: AsyncSession,
    email: str,
    username: str,
    password: str,
    role: Role = Role.USER,
) -> User:
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        auth_source="local",
        role=role,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(user)
    logger.info(
        "User registered: %s",
        username,
        extra={"action": "register", "email": email, "username": username},
    )

    await _auto_assign_not_grouped(db, user)
    return user


async def create_ldap_user(
    db: AsyncSession,
    email: str,
    username: str,
) -> User:
    user = User(
        email=email,
        username=username,
        hashed_password=None,
        auth_source="ldap",
        role=Role.USER,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(user)
    logger.info(
        "LDAP user provisioned: %s",
        username,
        extra={
            "action": "ldap_provision",
            "email": email,
            "username": username,
        },
    )
    await _auto_assign_not_grouped(db, user)
    return user


async def set_user_role(
    db: AsyncSession,
    target_user_id: uuid.UUID,
    new_role: Role,
    modified_by: uuid.UUID | None = None,
) -> User | None:
    user = await get_user_by_id(db, target_user_id)
    if not user:
        return None
    user.role = new_role
    if modified_by is not None:
        user.modified_by = modified_by
    await db.commit()
    await db.refresh(user)
    return user


async def _authenticate_local(db: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(db, email)
    if not user:
        verify_password(password, _DUMMY_HASH)
        logger.warning(
            "Login failed: unknown email",
            extra={"action": "login_failure", "email": email},
        )
        return None
    if user.auth_source != "local" or not user.hashed_password:
        # An LDAP-sourced user must not be able to log in via the local path.
        verify_password(password, _DUMMY_HASH)
        logger.warning(
            "Login failed: wrong auth source for %s (expected local, got %s)",
            email,
            user.auth_source,
            extra={
                "action": "login_failure",
                "email": email,
                "reason": "wrong_auth_source",
            },
        )
        return None
    if not verify_password(password, user.hashed_password):
        logger.warning(
            "Login failed: wrong password for %s",
            email,
            extra={"action": "login_failure", "email": email},
        )
        return None
    if not user.is_active:
        # Mirror the LDAP path and the refresh path: a deactivated account must
        # not be issued a fresh token even with correct credentials.
        logger.warning(
            "Login failed: account deactivated for %s",
            email,
            extra={"action": "login_failure", "email": email, "reason": "inactive"},
        )
        return None
    logger.info(
        "Login success: %s",
        user.username,
        extra={"action": "login_success", "user_id": str(user.id), "email": email},
    )
    return user


async def _authenticate_ldap(db: AsyncSession, email: str, password: str) -> User | None:
    # Clients still submit `email` in the login payload. For LDAP we treat it as
    # the short-form username (sAMAccountName) that gets injected into the
    # configured search filter.
    identity = await ldap_service.bind_user(email, password)
    if identity is None:
        logger.warning(
            "Login failed: LDAP bind rejected",
            extra={"action": "login_failure", "email": email, "source": "ldap"},
        )
        return None

    user = await get_user_by_email(db, identity.email)
    if user is None:
        try:
            user = await create_ldap_user(db, identity.email, identity.username)
        except IntegrityError:
            # Username collision with an existing local account; refuse rather
            # than silently take over a local identity.
            logger.warning(
                "LDAP provisioning collided with existing username",
                extra={
                    "action": "login_failure",
                    "email": identity.email,
                    "username": identity.username,
                    "reason": "username_collision",
                },
            )
            return None
    elif user.auth_source != "ldap":
        logger.warning(
            "Login failed: email matches a non-LDAP account",
            extra={
                "action": "login_failure",
                "email": identity.email,
                "reason": "wrong_auth_source",
            },
        )
        return None

    if not user.is_active:
        return None

    logger.info(
        "Login success (ldap): %s",
        user.username,
        extra={
            "action": "login_success",
            "user_id": str(user.id),
            "email": user.email,
            "source": "ldap",
        },
    )
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    if settings.auth_method == "ldap":
        return await _authenticate_ldap(db, email, password)
    return await _authenticate_local(db, email, password)


async def create_tokens_for_user(db: AsyncSession, user: User) -> tuple[str, str]:
    """Returns (access_token, raw_refresh_token)."""
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
    }
    access_token = create_access_token(payload)
    raw_refresh, token_hash, expires_at = create_refresh_token()

    db_token = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(db_token)
    await db.commit()
    return access_token, raw_refresh


async def rotate_refresh_token(db: AsyncSession, raw_refresh_token: str) -> tuple[str, str] | None:
    """Validates and rotates refresh token. Returns (access_token, new_refresh_token) or None."""
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,  # noqa: E712
        )
    )
    db_token = result.scalar_one_or_none()
    if not db_token:
        return None

    expires_at = db_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None

    user = await get_user_by_id(db, db_token.user_id)
    if not user or not user.is_active:
        return None

    db_token.revoked = True
    await db.commit()

    access_token, new_raw_refresh = await create_tokens_for_user(db, user)
    return access_token, new_raw_refresh


async def revoke_refresh_token(db: AsyncSession, raw_refresh_token: str) -> bool:
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    db_token = result.scalar_one_or_none()
    if not db_token:
        return False
    db_token.revoked = True
    await db.commit()
    return True
