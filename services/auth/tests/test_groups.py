import uuid

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.group import UserGroup
from app.models.user import Role, User
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


_admin_id = uuid.uuid4()
_superadmin_id = uuid.uuid4()
_user_id = uuid.uuid4()


def _make_mock_user(user_id: uuid.UUID, role: Role, username: str = "mock") -> User:
    return User(
        id=user_id,
        email=f"{username}@test.com",
        username=username,
        hashed_password="fake",
        is_active=True,
        role=role,
    )


@pytest.fixture
async def admin_client():
    mock_admin = _make_mock_user(_admin_id, Role.ADMIN, "admin")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: mock_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def superadmin_client():
    mock_sa = _make_mock_user(_superadmin_id, Role.SUPERADMIN, "superadmin")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: mock_sa
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    mock_user = _make_mock_user(_user_id, Role.USER, "regular")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: mock_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def no_auth_client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _create_group(client, name="Test Group", description="A test group"):
    resp = await client.post("/groups", json={"name": name, "description": description})
    return resp


async def _seed_user(db_session, user_id=None, username="seeduser", email="seed@test.com"):
    """Insert a user directly into the DB for membership tests."""
    uid = user_id or uuid.uuid4()
    user = User(
        id=uid,
        email=email,
        username=username,
        hashed_password="fake",
        is_active=True,
        role=Role.USER,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _seed_group(name="Test Group", description="A test group", created_by=None):
    """Insert a group directly into the DB."""
    async with TestSessionLocal() as session:
        group = UserGroup(
            name=name,
            description=description,
            created_by=created_by,
        )
        session.add(group)
        await session.commit()
        await session.refresh(group)
        return group


# --- Group CRUD ---


@pytest.mark.asyncio
async def test_create_group_admin(admin_client):
    resp = await _create_group(admin_client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Group"
    assert data["description"] == "A test group"
    assert data["created_by"] == str(_admin_id)


@pytest.mark.asyncio
async def test_create_group_superadmin(superadmin_client):
    resp = await _create_group(superadmin_client, name="SA Group")
    assert resp.status_code == 201
    assert resp.json()["created_by"] == str(_superadmin_id)


@pytest.mark.asyncio
async def test_create_group_user_forbidden(user_client):
    resp = await _create_group(user_client)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_group_unauthenticated(no_auth_client):
    resp = await no_auth_client.post("/groups", json={"name": "Fail"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_group_duplicate_name(admin_client):
    await _create_group(admin_client, name="Unique")
    resp = await _create_group(admin_client, name="Unique")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_group_empty_name(admin_client):
    resp = await admin_client.post("/groups", json={"name": "", "description": "nope"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_groups(user_client):
    # Seed groups via DB to avoid fixture conflicts
    await _seed_group(name="Group A")
    await _seed_group(name="Group B")
    resp = await user_client.get("/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "skip" in data
    assert "limit" in data
    # At least the 2 seeded groups (may also include "Not Grouped" default)
    names = [g["name"] for g in data["items"]]
    assert "Group A" in names
    assert "Group B" in names


@pytest.mark.asyncio
async def test_list_groups_empty(user_client):
    resp = await user_client.get("/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["items"], list)
    assert data["skip"] == 0
    assert data["limit"] == 50


@pytest.mark.asyncio
async def test_get_group(user_client):
    group = await _seed_group()
    resp = await user_client.get(f"/groups/{group.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Group"
    assert data["members"] == []


@pytest.mark.asyncio
async def test_get_group_not_found(user_client):
    resp = await user_client.get(f"/groups/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_group(admin_client):
    create_resp = await _create_group(admin_client)
    group_id = create_resp.json()["id"]
    resp = await admin_client.put(
        f"/groups/{group_id}",
        json={"name": "Updated Name", "description": "Updated desc"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["description"] == "Updated desc"


@pytest.mark.asyncio
async def test_update_group_duplicate_name(admin_client):
    await _create_group(admin_client, name="First")
    create_resp = await _create_group(admin_client, name="Second")
    group_id = create_resp.json()["id"]
    resp = await admin_client.put(f"/groups/{group_id}", json={"name": "First"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_group_not_found(admin_client):
    resp = await admin_client.put(f"/groups/{uuid.uuid4()}", json={"name": "Nope"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_group_user_forbidden(user_client):
    # 403 fires before route body, so group doesn't need to exist
    resp = await user_client.put(f"/groups/{uuid.uuid4()}", json={"name": "Nope"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_group(admin_client):
    create_resp = await _create_group(admin_client)
    group_id = create_resp.json()["id"]
    resp = await admin_client.delete(f"/groups/{group_id}")
    assert resp.status_code == 204
    # Verify gone
    resp = await admin_client.get(f"/groups/{group_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_group_not_found(admin_client):
    resp = await admin_client.delete(f"/groups/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_group_user_forbidden(user_client):
    resp = await user_client.delete(f"/groups/{uuid.uuid4()}")
    assert resp.status_code == 403


# --- Membership ---


@pytest.mark.asyncio
async def test_add_member(admin_client):
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="member1", email="member1@test.com")
        user_id = user.id

    create_resp = await _create_group(admin_client, name="Members Group")
    group_id = create_resp.json()["id"]

    resp = await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user_id)})
    assert resp.status_code == 201
    data = resp.json()
    assert data["user_id"] == str(user_id)
    assert data["username"] == "member1"


@pytest.mark.asyncio
async def test_add_member_duplicate(admin_client):
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="dup1", email="dup1@test.com")
        user_id = user.id

    create_resp = await _create_group(admin_client, name="Dup Group")
    group_id = create_resp.json()["id"]

    await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user_id)})
    resp = await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user_id)})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_add_member_user_not_found(admin_client):
    create_resp = await _create_group(admin_client, name="No User Group")
    group_id = create_resp.json()["id"]
    resp = await admin_client.post(
        f"/groups/{group_id}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404
    assert "User not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_member_group_not_found(admin_client):
    resp = await admin_client.post(
        f"/groups/{uuid.uuid4()}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404
    assert "Group not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_member_user_forbidden(user_client):
    resp = await user_client.post(
        f"/groups/{uuid.uuid4()}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_remove_member(admin_client):
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="removeme", email="removeme@test.com")
        user_id = user.id

    create_resp = await _create_group(admin_client, name="Remove Group")
    group_id = create_resp.json()["id"]

    await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user_id)})
    resp = await admin_client.delete(f"/groups/{group_id}/members/{user_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_remove_member_not_found(admin_client):
    create_resp = await _create_group(admin_client, name="No Member Group")
    group_id = create_resp.json()["id"]
    resp = await admin_client.delete(f"/groups/{group_id}/members/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_member_group_not_found(admin_client):
    resp = await admin_client.delete(f"/groups/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_member_user_forbidden(user_client):
    resp = await user_client.delete(f"/groups/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 403


# --- Edge cases ---


@pytest.mark.asyncio
async def test_get_group_with_members(admin_client):
    async with TestSessionLocal() as session:
        user1 = await _seed_user(session, username="u1", email="u1@test.com")
        user2 = await _seed_user(session, username="u2", email="u2@test.com")

    create_resp = await _create_group(admin_client, name="With Members")
    group_id = create_resp.json()["id"]

    await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user1.id)})
    await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user2.id)})

    resp = await admin_client.get(f"/groups/{group_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["members"]) == 2
    usernames = {m["username"] for m in data["members"]}
    assert usernames == {"u1", "u2"}


@pytest.mark.asyncio
async def test_cascade_delete_group_removes_members(admin_client):
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="cascade1", email="cascade1@test.com")

    create_resp = await _create_group(admin_client, name="Cascade Group")
    group_id = create_resp.json()["id"]
    await admin_client.post(f"/groups/{group_id}/members", json={"user_id": str(user.id)})

    resp = await admin_client.delete(f"/groups/{group_id}")
    assert resp.status_code == 204

    # Group is gone
    resp = await admin_client.get(f"/groups/{group_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_in_multiple_groups(admin_client):
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="multi1", email="multi1@test.com")

    resp1 = await _create_group(admin_client, name="Group X")
    resp2 = await _create_group(admin_client, name="Group Y")
    gid1 = resp1.json()["id"]
    gid2 = resp2.json()["id"]

    await admin_client.post(f"/groups/{gid1}/members", json={"user_id": str(user.id)})
    await admin_client.post(f"/groups/{gid2}/members", json={"user_id": str(user.id)})

    # Verify user appears in both groups
    r1 = await admin_client.get(f"/groups/{gid1}")
    r2 = await admin_client.get(f"/groups/{gid2}")
    assert len(r1.json()["members"]) == 1
    assert len(r2.json()["members"]) == 1


@pytest.mark.asyncio
async def test_create_group_no_description(admin_client):
    resp = await admin_client.post("/groups", json={"name": "No Desc"})
    assert resp.status_code == 201
    assert resp.json()["description"] is None


@pytest.mark.asyncio
async def test_update_group_partial_name_only(admin_client):
    create_resp = await _create_group(admin_client, name="Partial", description="Original")
    group_id = create_resp.json()["id"]
    resp = await admin_client.put(f"/groups/{group_id}", json={"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    # description unchanged (None sent, so not updated)
    assert resp.json()["description"] == "Original"


# --- User Groups Endpoint ---


@pytest.mark.asyncio
async def test_get_user_groups(admin_client):
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="grpuser", email="grpuser@test.com")

    resp1 = await _create_group(admin_client, name="UG1")
    resp2 = await _create_group(admin_client, name="UG2")
    gid1 = resp1.json()["id"]
    gid2 = resp2.json()["id"]

    await admin_client.post(f"/groups/{gid1}/members", json={"user_id": str(user.id)})
    await admin_client.post(f"/groups/{gid2}/members", json={"user_id": str(user.id)})

    resp = await admin_client.get(f"/groups/user/{user.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {g["name"] for g in data}
    assert names == {"UG1", "UG2"}


@pytest.mark.asyncio
async def test_get_user_groups_empty(user_client):
    resp = await user_client.get(f"/groups/user/{_user_id}")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_user_groups_nonexistent_user(user_client):
    resp = await user_client.get(f"/groups/user/{uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json() == []


# --- Bulk member tests ---


@pytest.mark.asyncio
async def test_bulk_add_members_success(admin_client):
    group = await _seed_group("BulkAddGroup")
    async with TestSessionLocal() as db:
        u1 = await _seed_user(db, username="bulku1", email="bulku1@test.com")
        u2 = await _seed_user(db, username="bulku2", email="bulku2@test.com")

    resp = await admin_client.post(
        f"/groups/{group.id}/members/bulk",
        json={"user_ids": [str(u1.id), str(u2.id)]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 2
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_bulk_add_members_skips_duplicates(admin_client):
    group = await _seed_group("BulkDupGroup")
    async with TestSessionLocal() as db:
        u1 = await _seed_user(db, username="bulkdup1", email="bulkdup1@test.com")

    # Add once
    await admin_client.post(
        f"/groups/{group.id}/members/bulk",
        json={"user_ids": [str(u1.id)]},
    )
    # Add again
    resp = await admin_client.post(
        f"/groups/{group.id}/members/bulk",
        json={"user_ids": [str(u1.id)]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_bulk_add_members_empty_list(admin_client):
    group = await _seed_group("BulkEmptyGroup")
    resp = await admin_client.post(
        f"/groups/{group.id}/members/bulk",
        json={"user_ids": []},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 0
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_bulk_add_members_requires_admin(user_client):
    group = await _seed_group("BulkAuthGroup")
    resp = await user_client.post(
        f"/groups/{group.id}/members/bulk",
        json={"user_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bulk_remove_members_success(admin_client):
    group = await _seed_group("BulkRemGroup")
    async with TestSessionLocal() as db:
        u1 = await _seed_user(db, username="bulkrem1", email="bulkrem1@test.com")

    # Add first
    await admin_client.post(
        f"/groups/{group.id}/members/bulk",
        json={"user_ids": [str(u1.id)]},
    )
    # Remove
    resp = await admin_client.post(
        f"/groups/{group.id}/members/bulk-remove",
        json={"user_ids": [str(u1.id)]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["removed"] == 1
    assert result["not_found"] == 0


@pytest.mark.asyncio
async def test_bulk_remove_members_not_found(admin_client):
    group = await _seed_group("BulkRemNFGroup")
    resp = await admin_client.post(
        f"/groups/{group.id}/members/bulk-remove",
        json={"user_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["removed"] == 0
    assert result["not_found"] == 1


@pytest.mark.asyncio
async def test_bulk_remove_members_group_not_found(admin_client):
    resp = await admin_client.post(
        f"/groups/{uuid.uuid4()}/members/bulk-remove",
        json={"user_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 404


# --- Auto-removal from "Not Grouped" ---


@pytest.mark.asyncio
async def test_add_member_removes_from_not_grouped(admin_client):
    """Adding a user to a group should auto-remove them from 'Not Grouped'."""
    # Create "Not Grouped" default group
    ng_resp = await _create_group(admin_client, name="Not Grouped", description="Default")
    ng_id = ng_resp.json()["id"]

    # Create a user and add to "Not Grouped"
    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="ng_user1", email="ng_user1@test.com")

    await admin_client.post(f"/groups/{ng_id}/members", json={"user_id": str(user.id)})

    # Verify user is in "Not Grouped"
    ng_detail = await admin_client.get(f"/groups/{ng_id}")
    assert len(ng_detail.json()["members"]) == 1

    # Add user to another group
    other_resp = await _create_group(admin_client, name="Real Group")
    other_id = other_resp.json()["id"]
    await admin_client.post(f"/groups/{other_id}/members", json={"user_id": str(user.id)})

    # Verify user was removed from "Not Grouped"
    ng_detail = await admin_client.get(f"/groups/{ng_id}")
    assert len(ng_detail.json()["members"]) == 0

    # Verify user is in the real group
    other_detail = await admin_client.get(f"/groups/{other_id}")
    assert len(other_detail.json()["members"]) == 1


@pytest.mark.asyncio
async def test_bulk_add_removes_from_not_grouped(admin_client):
    """Bulk-adding users to a group should auto-remove them from 'Not Grouped'."""
    ng_resp = await _create_group(admin_client, name="Not Grouped", description="Default")
    ng_id = ng_resp.json()["id"]

    async with TestSessionLocal() as session:
        u1 = await _seed_user(session, username="ng_bulk1", email="ng_bulk1@test.com")
        u2 = await _seed_user(session, username="ng_bulk2", email="ng_bulk2@test.com")

    # Add both to "Not Grouped"
    await admin_client.post(
        f"/groups/{ng_id}/members/bulk",
        json={"user_ids": [str(u1.id), str(u2.id)]},
    )
    ng_detail = await admin_client.get(f"/groups/{ng_id}")
    assert len(ng_detail.json()["members"]) == 2

    # Bulk-add to another group
    other_resp = await _create_group(admin_client, name="Bulk Real Group")
    other_id = other_resp.json()["id"]
    await admin_client.post(
        f"/groups/{other_id}/members/bulk",
        json={"user_ids": [str(u1.id), str(u2.id)]},
    )

    # Verify removed from "Not Grouped"
    ng_detail = await admin_client.get(f"/groups/{ng_id}")
    assert len(ng_detail.json()["members"]) == 0


@pytest.mark.asyncio
async def test_add_to_not_grouped_does_not_remove(admin_client):
    """Adding a user to 'Not Grouped' itself should NOT trigger removal."""
    ng_resp = await _create_group(admin_client, name="Not Grouped", description="Default")
    ng_id = ng_resp.json()["id"]

    async with TestSessionLocal() as session:
        user = await _seed_user(session, username="ng_stay", email="ng_stay@test.com")

    await admin_client.post(f"/groups/{ng_id}/members", json={"user_id": str(user.id)})

    # User should still be in "Not Grouped"
    ng_detail = await admin_client.get(f"/groups/{ng_id}")
    assert len(ng_detail.json()["members"]) == 1
