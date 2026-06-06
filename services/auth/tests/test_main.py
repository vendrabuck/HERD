"""Tests for main.py: lifespan seeding functions."""

from unittest.mock import MagicMock, patch

import pytest
from app.database import Base
from app.models.group import UserGroup
from app.models.user import Role
from app.services.auth_service import create_user, get_user_by_email, superadmin_exists
from app.services.group_service import get_group_by_name
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


# --- _seed_superadmin ---


@pytest.mark.asyncio
async def test_seed_superadmin_skips_when_env_vars_empty():
    """When superadmin env vars are blank, _seed_superadmin returns early."""
    with (
        patch("app.main.settings") as mock_settings,
    ):
        mock_settings.superadmin_email = ""
        mock_settings.superadmin_username = ""
        mock_settings.superadmin_password = ""

        from app.main import _seed_superadmin

        await _seed_superadmin()

    # No superadmin should exist
    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False


@pytest.mark.asyncio
async def test_seed_superadmin_skips_when_already_exists():
    """When a superadmin already exists, _seed_superadmin is a no-op."""
    async with TestSessionLocal() as db:
        await create_user(db, "existing@test.com", "existingsa", "password123", Role.SUPERADMIN)

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "new@test.com"
        mock_settings.superadmin_username = "newsa"
        mock_settings.superadmin_password = "password123"

        from app.main import _seed_superadmin

        await _seed_superadmin()

    # Only one superadmin should exist (the original one)
    async with TestSessionLocal() as db:
        result = await get_user_by_email(db, "new@test.com")
        assert result is None


@pytest.mark.asyncio
async def test_seed_superadmin_skips_when_email_already_registered():
    """When email is already registered as non-superadmin, warns and skips."""
    async with TestSessionLocal() as db:
        await create_user(db, "taken@test.com", "takenuser", "password123", Role.USER)

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "taken@test.com"
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = "password123"

        from app.main import _seed_superadmin

        await _seed_superadmin()

    # User should still be regular, not superadmin
    async with TestSessionLocal() as db:
        user = await get_user_by_email(db, "taken@test.com")
        assert user is not None
        assert user.role == Role.USER


@pytest.mark.asyncio
async def test_seed_superadmin_creates_new_superadmin():
    """When conditions are met, creates the superadmin account."""
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "sa@test.com"
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = "strongpass1"

        from app.main import _seed_superadmin

        await _seed_superadmin()

    async with TestSessionLocal() as db:
        user = await get_user_by_email(db, "sa@test.com")
        assert user is not None
        assert user.role == Role.SUPERADMIN
        assert user.username == "superadmin"


@pytest.mark.asyncio
async def test_seed_superadmin_skips_partial_env_vars():
    """When only some env vars are set (password missing), skip seeding."""
    with (
        patch("app.main.settings") as mock_settings,
    ):
        mock_settings.superadmin_email = "sa@test.com"
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = ""

        from app.main import _seed_superadmin

        await _seed_superadmin()

    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False


# --- _seed_not_grouped ---


@pytest.mark.asyncio
async def test_seed_not_grouped_creates_default_group():
    """Creates 'Not Grouped' default user group on first run."""
    with patch("app.main.AsyncSessionLocal", TestSessionLocal):
        from app.main import _seed_not_grouped

        await _seed_not_grouped()

    async with TestSessionLocal() as db:
        group = await get_group_by_name(db, "Not Grouped")
        assert group is not None
        assert group.name == "Not Grouped"


@pytest.mark.asyncio
async def test_seed_not_grouped_skips_when_exists():
    """When 'Not Grouped' already exists, does not create a duplicate."""
    async with TestSessionLocal() as db:
        db.add(UserGroup(name="Not Grouped", description="Default"))
        await db.commit()

    with patch("app.main.AsyncSessionLocal", TestSessionLocal):
        from app.main import _seed_not_grouped

        await _seed_not_grouped()

    async with TestSessionLocal() as db:
        from sqlalchemy import func, select

        count = (
            await db.execute(
                select(func.count()).select_from(UserGroup).where(UserGroup.name == "Not Grouped")
            )
        ).scalar()
        assert count == 1


@pytest.mark.asyncio
async def test_seed_not_grouped_handles_integrity_error():
    """IntegrityError during insert is caught and rolled back."""
    # Pre-create the group so the insert will cause IntegrityError
    async with TestSessionLocal() as db:
        db.add(UserGroup(name="Not Grouped", description="Default"))
        await db.commit()

    # Patch get_group_by_name to return None so it attempts the insert
    original_get = get_group_by_name

    async def fake_get(db, name):
        if name == "Not Grouped":
            return None
        return await original_get(db, name)

    with (
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
        patch("app.services.group_service.get_group_by_name", side_effect=fake_get),
    ):
        from app.main import _seed_not_grouped

        # Should not raise; IntegrityError is caught
        await _seed_not_grouped()


# --- lifespan ---


@pytest.mark.asyncio
async def test_lifespan_runs_seeding():
    """lifespan creates tables and runs seed functions."""
    from app.main import lifespan

    seed_sa_called = False
    seed_ng_called = False

    async def mock_seed_sa():
        nonlocal seed_sa_called
        seed_sa_called = True

    async def mock_seed_ng():
        nonlocal seed_ng_called
        seed_ng_called = True

    with (
        patch("app.main.engine", engine),
        patch("app.main._seed_superadmin", mock_seed_sa),
        patch("app.main._seed_not_grouped", mock_seed_ng),
    ):
        mock_app = MagicMock()
        async with lifespan(mock_app):
            pass

    assert seed_sa_called
    assert seed_ng_called
