import uuid
from datetime import datetime, timezone

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RefreshToken, Role, User
from app.utils.jwt import create_access_token, create_refresh_token, hash_token

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


async def get_all_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at))
    return list(result.scalars().all())


async def superadmin_exists(db: AsyncSession) -> bool:
    result = await db.execute(select(User).where(User.role == Role.SUPERADMIN))
    return result.scalar_one_or_none() is not None


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
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def set_user_role(
    db: AsyncSession,
    target_user_id: uuid.UUID,
    new_role: Role,
) -> User | None:
    user = await get_user_by_id(db, target_user_id)
    if not user:
        return None
    user.role = new_role
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(db, email)
    if not user:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


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


async def rotate_refresh_token(
    db: AsyncSession, raw_refresh_token: str
) -> tuple[str, str] | None:
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

    if db_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None

    user = await get_user_by_id(db, db_token.user_id)
    if not user or not user.is_active:
        return None

    db_token.revoked = True
    await db.flush()

    access_token, new_raw_refresh = await create_tokens_for_user(db, user)
    return access_token, new_raw_refresh


async def revoke_refresh_token(db: AsyncSession, raw_refresh_token: str) -> bool:
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    db_token = result.scalar_one_or_none()
    if not db_token:
        return False
    db_token.revoked = True
    await db.commit()
    return True
