"""Unit tests for group_service.py functions."""

import uuid

import pytest
from app.database import Base
from app.services.auth_service import create_user
from app.services.group_service import (
    add_member,
    bulk_add_members,
    bulk_remove_members,
    create_group,
    delete_group,
    get_all_groups,
    get_group_by_id,
    get_group_by_name,
    get_group_members,
    get_user_groups,
    remove_member,
    update_group,
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


async def _make_user(db, email, username):
    return await create_user(db, email, username, "password123")


async def _make_group(db, name="Test Group", created_by=None):
    if created_by is None:
        created_by = uuid.uuid4()
    return await create_group(db, name, f"{name} description", created_by)


# --- create_group ---


@pytest.mark.asyncio
async def test_create_group_success():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Alpha")
        assert group.name == "Alpha"


@pytest.mark.asyncio
async def test_create_group_duplicate_name():
    async with TestSessionLocal() as db:
        await _make_group(db, "Dup")
        with pytest.raises(IntegrityError):
            await _make_group(db, "Dup")


# --- get_all_groups ---


@pytest.mark.asyncio
async def test_get_all_groups_empty():
    async with TestSessionLocal() as db:
        groups, total = await get_all_groups(db)
        assert groups == []
        assert total == 0


@pytest.mark.asyncio
async def test_get_all_groups_pagination():
    async with TestSessionLocal() as db:
        for i in range(5):
            await _make_group(db, f"Group {i}")
        groups, total = await get_all_groups(db, skip=1, limit=2)
        assert len(groups) == 2
        assert total == 5


# --- get_group_by_id ---


@pytest.mark.asyncio
async def test_get_group_by_id_not_found():
    async with TestSessionLocal() as db:
        result = await get_group_by_id(db, uuid.uuid4())
        assert result is None


# --- get_group_by_name ---


@pytest.mark.asyncio
async def test_get_group_by_name_not_found():
    async with TestSessionLocal() as db:
        result = await get_group_by_name(db, "Nonexistent")
        assert result is None


@pytest.mark.asyncio
async def test_get_group_by_name_found():
    async with TestSessionLocal() as db:
        await _make_group(db, "FindMe")
        result = await get_group_by_name(db, "FindMe")
        assert result is not None
        assert result.name == "FindMe"


# --- update_group ---


@pytest.mark.asyncio
async def test_update_group_partial_fields():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Original")
        updated = await update_group(db, group, name="Renamed", description=None)
        assert updated.name == "Renamed"
        assert updated.description == "Original description"


@pytest.mark.asyncio
async def test_update_group_with_modified_by():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Mod")
        modifier = uuid.uuid4()
        updated = await update_group(db, group, name=None, description="new", modified_by=modifier)
        assert updated.modified_by == modifier


@pytest.mark.asyncio
async def test_update_group_duplicate_name():
    async with TestSessionLocal() as db:
        await _make_group(db, "Taken")
        group2 = await _make_group(db, "Other")
        with pytest.raises(IntegrityError):
            await update_group(db, group2, name="Taken", description=None)


# --- delete_group ---


@pytest.mark.asyncio
async def test_delete_group():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "ToDelete")
        await delete_group(db, group)
        result = await get_group_by_name(db, "ToDelete")
        assert result is None


# --- add_member / remove_member ---


@pytest.mark.asyncio
async def test_add_member_success():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Team")
        user = await _make_user(db, "m@t.com", "member")
        member = await add_member(db, group.id, user.id)
        assert member.user_id == user.id


@pytest.mark.asyncio
async def test_add_member_duplicate():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Team")
        user = await _make_user(db, "m@t.com", "member")
        await add_member(db, group.id, user.id)
        with pytest.raises(IntegrityError):
            await add_member(db, group.id, user.id)


@pytest.mark.asyncio
async def test_remove_member_success():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Team")
        user = await _make_user(db, "m@t.com", "member")
        await add_member(db, group.id, user.id)
        result = await remove_member(db, group.id, user.id)
        assert result is True


@pytest.mark.asyncio
async def test_remove_member_not_found():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Team")
        result = await remove_member(db, group.id, uuid.uuid4())
        assert result is False


# --- get_group_members ---


@pytest.mark.asyncio
async def test_get_group_members_returns_user_info():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Team")
        user = await _make_user(db, "m@t.com", "member")
        await add_member(db, group.id, user.id)
        members = await get_group_members(db, group.id)
        assert len(members) == 1
        assert members[0]["username"] == "member"
        assert members[0]["email"] == "m@t.com"


@pytest.mark.asyncio
async def test_get_group_members_empty():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Empty")
        members = await get_group_members(db, group.id)
        assert members == []


# --- get_user_groups ---


@pytest.mark.asyncio
async def test_get_user_groups_empty():
    async with TestSessionLocal() as db:
        user = await _make_user(db, "u@t.com", "lonely")
        groups = await get_user_groups(db, user.id)
        assert groups == []


@pytest.mark.asyncio
async def test_get_user_groups_returns_groups():
    async with TestSessionLocal() as db:
        user = await _make_user(db, "u@t.com", "member")
        g1 = await _make_group(db, "Alpha")
        g2 = await _make_group(db, "Beta")
        await add_member(db, g1.id, user.id)
        await add_member(db, g2.id, user.id)
        groups = await get_user_groups(db, user.id)
        names = {g.name for g in groups}
        assert "Alpha" in names
        assert "Beta" in names


# --- bulk_add_members ---


@pytest.mark.asyncio
async def test_bulk_add_members_mixed():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Bulk")
        u1 = await _make_user(db, "u1@t.com", "user1")
        u2 = await _make_user(db, "u2@t.com", "user2")
        await add_member(db, group.id, u1.id)
        added, skipped = await bulk_add_members(db, group.id, [u1.id, u2.id])
        assert added == 1
        assert skipped == 1


@pytest.mark.asyncio
async def test_bulk_add_members_all_new():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Bulk")
        u1 = await _make_user(db, "u1@t.com", "user1")
        u2 = await _make_user(db, "u2@t.com", "user2")
        added, skipped = await bulk_add_members(db, group.id, [u1.id, u2.id])
        assert added == 2
        assert skipped == 0


# --- bulk_remove_members ---


@pytest.mark.asyncio
async def test_bulk_remove_members_mixed():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Bulk")
        u1 = await _make_user(db, "u1@t.com", "user1")
        await add_member(db, group.id, u1.id)
        removed, not_found = await bulk_remove_members(db, group.id, [u1.id, uuid.uuid4()])
        assert removed == 1
        assert not_found == 1


@pytest.mark.asyncio
async def test_bulk_remove_members_all_not_found():
    async with TestSessionLocal() as db:
        group = await _make_group(db, "Bulk")
        removed, not_found = await bulk_remove_members(db, group.id, [uuid.uuid4(), uuid.uuid4()])
        assert removed == 0
        assert not_found == 2


# --- Auto-removal from "Not Grouped" ---


@pytest.mark.asyncio
async def test_add_member_removes_from_not_grouped():
    """Adding a user to a non-default group auto-removes them from 'Not Grouped'."""
    async with TestSessionLocal() as db:
        # Create user BEFORE "Not Grouped" exists so auto-assign does not fire
        user = await _make_user(db, "ng@t.com", "nguser")
        ng = await create_group(db, "Not Grouped", "Default", uuid.uuid4())
        other = await create_group(db, "Real Group", "desc", uuid.uuid4())
        # Manually add to "Not Grouped"
        await add_member(db, ng.id, user.id)
        members = await get_group_members(db, ng.id)
        assert len(members) == 1
        # Add to another group; should auto-remove from "Not Grouped"
        await add_member(db, other.id, user.id)
        members = await get_group_members(db, ng.id)
        assert len(members) == 0


@pytest.mark.asyncio
async def test_bulk_add_removes_from_not_grouped():
    """Bulk-adding users to a non-default group auto-removes from 'Not Grouped'."""
    async with TestSessionLocal() as db:
        # Create users BEFORE "Not Grouped" exists
        u1 = await _make_user(db, "b1@t.com", "bulkng1")
        u2 = await _make_user(db, "b2@t.com", "bulkng2")
        ng = await create_group(db, "Not Grouped", "Default", uuid.uuid4())
        other = await create_group(db, "Another", "desc", uuid.uuid4())
        # Add both to "Not Grouped"
        await bulk_add_members(db, ng.id, [u1.id, u2.id])
        members = await get_group_members(db, ng.id)
        assert len(members) == 2
        # Bulk-add to another group
        await bulk_add_members(db, other.id, [u1.id, u2.id])
        members = await get_group_members(db, ng.id)
        assert len(members) == 0
