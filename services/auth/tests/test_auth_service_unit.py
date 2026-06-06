"""Unit tests for auth_service.py functions."""

import uuid

import pytest
from app.database import Base
from app.models.user import Role
from app.services.auth_service import (
    authenticate_user,
    create_tokens_for_user,
    create_user,
    get_all_users,
    get_password_hash,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    revoke_refresh_token,
    rotate_refresh_token,
    set_user_role,
    superadmin_exists,
    verify_password,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _get_session():
    async with TestSessionLocal() as session:
        yield session


async def _create_test_user(db, email="test@test.com", username="testuser", role=Role.USER):
    return await create_user(db, email, username, "password123", role)


# --- Password hashing ---


def test_verify_password_correct():
    hashed = get_password_hash("mysecret")
    assert verify_password("mysecret", hashed) is True


def test_verify_password_incorrect():
    hashed = get_password_hash("mysecret")
    assert verify_password("wrongpass", hashed) is False


def test_get_password_hash_produces_verifiable_hash():
    password = "test-password-123"
    hashed = get_password_hash(password)
    assert hashed != password
    assert verify_password(password, hashed) is True


# --- User lookups ---


@pytest.mark.asyncio
async def test_get_user_by_email_not_found():
    async with TestSessionLocal() as db:
        result = await get_user_by_email(db, "nonexistent@test.com")
        assert result is None


@pytest.mark.asyncio
async def test_get_user_by_username_not_found():
    async with TestSessionLocal() as db:
        result = await get_user_by_username(db, "nonexistent")
        assert result is None


@pytest.mark.asyncio
async def test_get_user_by_id_not_found():
    async with TestSessionLocal() as db:
        result = await get_user_by_id(db, uuid.uuid4())
        assert result is None


@pytest.mark.asyncio
async def test_get_user_by_email_found():
    async with TestSessionLocal() as db:
        user = await _create_test_user(db)
        result = await get_user_by_email(db, "test@test.com")
        assert result is not None
        assert result.id == user.id


@pytest.mark.asyncio
async def test_get_user_by_username_found():
    async with TestSessionLocal() as db:
        user = await _create_test_user(db)
        result = await get_user_by_username(db, "testuser")
        assert result is not None
        assert result.id == user.id


# --- get_all_users ---


@pytest.mark.asyncio
async def test_get_all_users_empty_db():
    async with TestSessionLocal() as db:
        users, total = await get_all_users(db)
        assert users == []
        assert total == 0


@pytest.mark.asyncio
async def test_get_all_users_with_pagination():
    async with TestSessionLocal() as db:
        for i in range(5):
            await create_user(db, f"user{i}@test.com", f"user{i}", f"password{i}x")
        users, total = await get_all_users(db, skip=1, limit=2)
        assert len(users) == 2
        assert total == 5


# --- superadmin_exists ---


@pytest.mark.asyncio
async def test_superadmin_exists_false():
    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False


@pytest.mark.asyncio
async def test_superadmin_exists_true():
    async with TestSessionLocal() as db:
        await create_user(db, "sa@test.com", "superadmin", "password123", Role.SUPERADMIN)
        assert await superadmin_exists(db) is True


# --- create_user ---


@pytest.mark.asyncio
async def test_create_user_duplicate_email():
    async with TestSessionLocal() as db:
        await _create_test_user(db)
        with pytest.raises(IntegrityError):
            await create_user(db, "test@test.com", "other", "password123")


@pytest.mark.asyncio
async def test_create_user_assigns_role():
    async with TestSessionLocal() as db:
        user = await create_user(db, "admin@test.com", "admin", "password123", Role.ADMIN)
        assert user.role == Role.ADMIN


# --- set_user_role ---


@pytest.mark.asyncio
async def test_set_user_role_nonexistent_user():
    async with TestSessionLocal() as db:
        result = await set_user_role(db, uuid.uuid4(), Role.ADMIN)
        assert result is None


@pytest.mark.asyncio
async def test_set_user_role_success():
    async with TestSessionLocal() as db:
        user = await _create_test_user(db)
        modifier_id = uuid.uuid4()
        result = await set_user_role(db, user.id, Role.ADMIN, modified_by=modifier_id)
        assert result is not None
        assert result.role == Role.ADMIN
        assert result.modified_by == modifier_id


# --- authenticate_user ---


@pytest.mark.asyncio
async def test_authenticate_user_success():
    async with TestSessionLocal() as db:
        await _create_test_user(db, "auth@test.com", "authuser")
        result = await authenticate_user(db, "auth@test.com", "password123")
        assert result is not None
        assert result.username == "authuser"


@pytest.mark.asyncio
async def test_authenticate_user_rejects_deactivated_local_user():
    """A deactivated local user must not authenticate even with the right password
    (regression: _authenticate_local previously skipped the is_active check that
    the LDAP and refresh paths enforce)."""
    async with TestSessionLocal() as db:
        user = await _create_test_user(db, "inactive@test.com", "inactiveuser")
        user.is_active = False
        await db.commit()
        result = await authenticate_user(db, "inactive@test.com", "password123")
        assert result is None


@pytest.mark.asyncio
async def test_authenticate_user_wrong_password():
    async with TestSessionLocal() as db:
        await _create_test_user(db, "auth@test.com", "authuser")
        result = await authenticate_user(db, "auth@test.com", "wrongpass")
        assert result is None


@pytest.mark.asyncio
async def test_authenticate_user_unknown_email():
    async with TestSessionLocal() as db:
        result = await authenticate_user(db, "nobody@test.com", "password123")
        assert result is None


# --- Token operations ---


@pytest.mark.asyncio
async def test_create_tokens_for_user():
    async with TestSessionLocal() as db:
        user = await _create_test_user(db)
        access, refresh = await create_tokens_for_user(db, user)
        assert isinstance(access, str)
        assert isinstance(refresh, str)
        assert len(access) > 0
        assert len(refresh) > 0


@pytest.mark.asyncio
async def test_rotate_refresh_token_success():
    async with TestSessionLocal() as db:
        user = await _create_test_user(db)
        _, raw_refresh = await create_tokens_for_user(db, user)
        result = await rotate_refresh_token(db, raw_refresh)
        assert result is not None
        new_access, new_refresh = result
        assert isinstance(new_access, str)
        assert isinstance(new_refresh, str)


@pytest.mark.asyncio
async def test_rotate_refresh_token_invalid():
    async with TestSessionLocal() as db:
        result = await rotate_refresh_token(db, "invalid-token")
        assert result is None


@pytest.mark.asyncio
async def test_revoke_refresh_token_success():
    async with TestSessionLocal() as db:
        user = await _create_test_user(db)
        _, raw_refresh = await create_tokens_for_user(db, user)
        assert await revoke_refresh_token(db, raw_refresh) is True
        # Cannot rotate after revocation
        assert await rotate_refresh_token(db, raw_refresh) is None


@pytest.mark.asyncio
async def test_revoke_refresh_token_not_found():
    async with TestSessionLocal() as db:
        assert await revoke_refresh_token(db, "nonexistent") is False


# --- rotate_refresh_token edge cases ---


@pytest.mark.asyncio
async def test_rotate_refresh_token_expired():
    """A refresh token with past expires_at should return None."""
    from datetime import datetime, timedelta, timezone

    from app.models.user import RefreshToken
    from app.utils.jwt import hash_token
    from sqlalchemy import update

    async with TestSessionLocal() as db:
        user = await _create_test_user(db, "expired@test.com", "expireduser")
        _, raw_refresh = await create_tokens_for_user(db, user)
        # Expire the token in DB
        token_hash = hash_token(raw_refresh)
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await db.commit()
        result = await rotate_refresh_token(db, raw_refresh)
        assert result is None


@pytest.mark.asyncio
async def test_rotate_refresh_token_inactive_user():
    """A refresh token for an inactive user should return None."""
    from app.models.user import User
    from sqlalchemy import update as sa_update

    async with TestSessionLocal() as db:
        user = await _create_test_user(db, "inactive@test.com", "inactiveuser")
        _, raw_refresh = await create_tokens_for_user(db, user)
        # Deactivate the user
        await db.execute(sa_update(User).where(User.id == user.id).values(is_active=False))
        await db.commit()
        result = await rotate_refresh_token(db, raw_refresh)
        assert result is None


# --- create_user auto-assign to "Not Grouped" ---


@pytest.mark.asyncio
async def test_create_user_auto_assigns_to_not_grouped():
    """When 'Not Grouped' exists, new users are auto-assigned."""
    from app.models.group import UserGroup
    from app.services.group_service import get_user_groups

    async with TestSessionLocal() as db:
        # Create the "Not Grouped" group first
        ng = UserGroup(name="Not Grouped", description="Default")
        db.add(ng)
        await db.commit()

        user = await create_user(db, "auto@test.com", "autouser", "password123")
        groups = await get_user_groups(db, user.id)
        names = {g.name for g in groups}
        assert "Not Grouped" in names


@pytest.mark.asyncio
async def test_create_user_handles_not_grouped_failure():
    """create_user does not fail if auto-assign to 'Not Grouped' raises."""
    from unittest.mock import patch

    from app.models.group import UserGroup

    async with TestSessionLocal() as db:
        # Create "Not Grouped" so get_group_by_name returns it
        ng = UserGroup(name="Not Grouped", description="Default")
        db.add(ng)
        await db.commit()

        # Patch add_member to raise so the except path is exercised
        async def failing_add(*args, **kwargs):
            raise RuntimeError("Simulated failure")

        with patch("app.services.group_service.add_member", side_effect=failing_add):
            # Should not raise; user is still created
            user = await create_user(db, "failng@test.com", "failnguser", "password123")
            assert user.email == "failng@test.com"
