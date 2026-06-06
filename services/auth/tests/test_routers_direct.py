"""Direct router function tests to ensure coverage of router code paths.

pytest-cov does not track code executed inside httpx ASGITransport, so these
tests call router handler functions directly with real DB sessions and mock
auth dependencies. This covers router-level logic (validation, HTTPException
branching, response construction) that the integration-style tests in
test_auth.py and test_groups.py already verify behaviorally.
"""

import uuid

import pytest
from app.database import Base, get_db
from app.models.user import Role, User
from app.services.auth_service import create_user
from app.services.group_service import add_member, create_group
from fastapi import HTTPException
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


def _mock_user(role=Role.USER, user_id=None, username="mockuser", email="mock@test.com"):
    return User(
        id=user_id or uuid.uuid4(),
        email=email,
        username=username,
        hashed_password="fake",
        is_active=True,
        role=role,
    )


# --- auth router: register ---


@pytest.mark.asyncio
async def test_register_endpoint_success():
    from app.routers.auth import register
    from app.schemas.auth import RegisterRequest

    async with TestSessionLocal() as db:
        body = RegisterRequest(email="new@test.com", username="newuser", password="password123")
        result = await register(body, db)
        assert result.email == "new@test.com"
        assert result.username == "newuser"
        assert result.role == Role.USER


@pytest.mark.asyncio
async def test_register_endpoint_duplicate_email():
    from app.routers.auth import register
    from app.schemas.auth import RegisterRequest

    async with TestSessionLocal() as db:
        await create_user(db, "dup@test.com", "first", "password123")
        body = RegisterRequest(email="dup@test.com", username="second", password="password123")
        with pytest.raises(HTTPException) as exc:
            await register(body, db)
        assert exc.value.status_code == 409
        assert "Email" in exc.value.detail


@pytest.mark.asyncio
async def test_register_endpoint_duplicate_username():
    from app.routers.auth import register
    from app.schemas.auth import RegisterRequest

    async with TestSessionLocal() as db:
        await create_user(db, "first@test.com", "sameuser", "password123")
        body = RegisterRequest(email="second@test.com", username="sameuser", password="password123")
        with pytest.raises(HTTPException) as exc:
            await register(body, db)
        assert exc.value.status_code == 409
        assert "Username" in exc.value.detail


@pytest.mark.asyncio
async def test_register_endpoint_integrity_error_fallback():
    """Race condition: email/username check passes but insert fails with IntegrityError."""
    from unittest.mock import patch

    from app.routers.auth import register
    from app.schemas.auth import RegisterRequest
    from sqlalchemy.exc import IntegrityError

    async with TestSessionLocal() as db:
        body = RegisterRequest(email="race@test.com", username="raceuser", password="password123")

        async def mock_create_user(db, email, username, password):
            raise IntegrityError("duplicate", params=None, orig=None)

        with patch("app.routers.auth.create_user", side_effect=mock_create_user):
            with pytest.raises(HTTPException) as exc:
                await register(body, db)
            assert exc.value.status_code == 409


# --- auth router: login ---


@pytest.mark.asyncio
async def test_login_endpoint_success():
    from app.routers.auth import login
    from app.schemas.auth import LoginRequest

    async with TestSessionLocal() as db:
        await create_user(db, "login@test.com", "loginuser", "password123")
        body = LoginRequest(email="login@test.com", password="password123")
        result = await login(body, db)
        assert hasattr(result, "access_token")
        assert hasattr(result, "refresh_token")


@pytest.mark.asyncio
async def test_login_endpoint_wrong_password():
    from app.routers.auth import login
    from app.schemas.auth import LoginRequest

    async with TestSessionLocal() as db:
        await create_user(db, "login@test.com", "loginuser", "password123")
        body = LoginRequest(email="login@test.com", password="wrongpassword")
        with pytest.raises(HTTPException) as exc:
            await login(body, db)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_endpoint_nonexistent_email():
    from app.routers.auth import login
    from app.schemas.auth import LoginRequest

    async with TestSessionLocal() as db:
        body = LoginRequest(email="nobody@test.com", password="password123")
        with pytest.raises(HTTPException) as exc:
            await login(body, db)
        assert exc.value.status_code == 401


# --- auth router: refresh ---


@pytest.mark.asyncio
async def test_refresh_endpoint_success():
    from app.routers.auth import refresh
    from app.schemas.auth import RefreshRequest
    from app.services.auth_service import create_tokens_for_user

    async with TestSessionLocal() as db:
        user = await create_user(db, "ref@test.com", "refuser", "password123")
        _, raw_refresh = await create_tokens_for_user(db, user)
        body = RefreshRequest(refresh_token=raw_refresh)
        result = await refresh(body, db)
        assert hasattr(result, "access_token")
        assert hasattr(result, "refresh_token")


@pytest.mark.asyncio
async def test_refresh_endpoint_invalid_token():
    from app.routers.auth import refresh
    from app.schemas.auth import RefreshRequest

    async with TestSessionLocal() as db:
        body = RefreshRequest(refresh_token="invalid-token-value")
        with pytest.raises(HTTPException) as exc:
            await refresh(body, db)
        assert exc.value.status_code == 401


# --- auth router: logout ---


@pytest.mark.asyncio
async def test_logout_endpoint():
    from app.routers.auth import logout
    from app.schemas.auth import LogoutRequest
    from app.services.auth_service import create_tokens_for_user

    async with TestSessionLocal() as db:
        user = await create_user(db, "out@test.com", "outuser", "password123")
        _, raw_refresh = await create_tokens_for_user(db, user)
        body = LogoutRequest(refresh_token=raw_refresh)
        # Should not raise; returns None (204 no content)
        result = await logout(body, db)
        assert result is None


# --- auth router: me ---


@pytest.mark.asyncio
async def test_me_endpoint():
    from app.routers.auth import me

    mock_user = _mock_user()
    result = await me(mock_user)
    assert result.email == "mock@test.com"


# --- admin router: list_users ---


@pytest.mark.asyncio
async def test_list_users_endpoint():
    from app.routers.admin import list_users

    async with TestSessionLocal() as db:
        await create_user(db, "u1@test.com", "user1", "password123")
        await create_user(db, "u2@test.com", "user2", "password123")
        result = await list_users(skip=0, limit=50, db=db, _=_mock_user(Role.ADMIN))
        assert result.total == 2
        assert len(result.items) == 2


@pytest.mark.asyncio
async def test_list_users_pagination():
    from app.routers.admin import list_users

    async with TestSessionLocal() as db:
        for i in range(5):
            await create_user(db, f"p{i}@test.com", f"puser{i}", "password123")
        result = await list_users(skip=2, limit=2, db=db, _=_mock_user(Role.ADMIN))
        assert result.total == 5
        assert len(result.items) == 2


# --- admin router: update_user_role ---


@pytest.mark.asyncio
async def test_update_user_role_success():
    from app.routers.admin import update_user_role
    from app.schemas.auth import SetRoleRequest

    sa = _mock_user(Role.SUPERADMIN)
    async with TestSessionLocal() as db:
        user = await create_user(db, "target@test.com", "target", "password123")
        body = SetRoleRequest(role=Role.ADMIN)
        result = await update_user_role(user.id, body, db=db, current_user=sa)
        assert result.role == Role.ADMIN


@pytest.mark.asyncio
async def test_update_user_role_cannot_set_superadmin():
    from app.routers.admin import update_user_role
    from app.schemas.auth import SetRoleRequest

    sa = _mock_user(Role.SUPERADMIN)
    async with TestSessionLocal() as db:
        user = await create_user(db, "t@test.com", "target", "password123")
        body = SetRoleRequest(role=Role.SUPERADMIN)
        with pytest.raises(HTTPException) as exc:
            await update_user_role(user.id, body, db=db, current_user=sa)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_user_role_cannot_change_own():
    from app.routers.admin import update_user_role
    from app.schemas.auth import SetRoleRequest

    sa_id = uuid.uuid4()
    sa = _mock_user(Role.SUPERADMIN, user_id=sa_id)
    async with TestSessionLocal() as db:
        body = SetRoleRequest(role=Role.USER)
        with pytest.raises(HTTPException) as exc:
            await update_user_role(sa_id, body, db=db, current_user=sa)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_user_role_not_found():
    from app.routers.admin import update_user_role
    from app.schemas.auth import SetRoleRequest

    sa = _mock_user(Role.SUPERADMIN)
    async with TestSessionLocal() as db:
        body = SetRoleRequest(role=Role.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await update_user_role(uuid.uuid4(), body, db=db, current_user=sa)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_user_role_cannot_change_superadmin_role():
    from app.routers.admin import update_user_role
    from app.schemas.auth import SetRoleRequest

    sa = _mock_user(Role.SUPERADMIN, user_id=uuid.uuid4())
    async with TestSessionLocal() as db:
        target = await create_user(db, "sa2@test.com", "sa2", "password123", Role.SUPERADMIN)
        body = SetRoleRequest(role=Role.USER)
        with pytest.raises(HTTPException) as exc:
            await update_user_role(target.id, body, db=db, current_user=sa)
        assert exc.value.status_code == 400
        assert "superadmin" in exc.value.detail.lower()


# --- groups router: create_group ---


@pytest.mark.asyncio
async def test_groups_create_endpoint():
    from app.routers.groups import create_group_endpoint
    from app.schemas.group import GroupCreateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = GroupCreateRequest(name="New Group", description="Desc")
        result = await create_group_endpoint(body, db=db, current_user=admin)
        assert result.name == "New Group"
        assert result.created_by == admin.id


@pytest.mark.asyncio
async def test_groups_create_duplicate_name():
    from app.routers.groups import create_group_endpoint
    from app.schemas.group import GroupCreateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = GroupCreateRequest(name="Unique", description="Desc")
        await create_group_endpoint(body, db=db, current_user=admin)
        with pytest.raises(HTTPException) as exc:
            await create_group_endpoint(body, db=db, current_user=admin)
        assert exc.value.status_code == 409


# --- groups router: list_groups ---


@pytest.mark.asyncio
async def test_groups_list_endpoint():
    from app.routers.groups import list_groups

    async with TestSessionLocal() as db:
        await create_group(db, "G1", "d", uuid.uuid4())
        await create_group(db, "G2", "d", uuid.uuid4())
        result = await list_groups(skip=0, limit=50, db=db, _=_mock_user())
        assert result.total == 2
        assert len(result.items) == 2


@pytest.mark.asyncio
async def test_groups_list_pagination():
    from app.routers.groups import list_groups

    async with TestSessionLocal() as db:
        for i in range(5):
            await create_group(db, f"Grp{i}", "d", uuid.uuid4())
        result = await list_groups(skip=1, limit=2, db=db, _=_mock_user())
        assert result.total == 5
        assert len(result.items) == 2


# --- groups router: get_group ---


@pytest.mark.asyncio
async def test_groups_get_endpoint():
    from app.routers.groups import get_group

    async with TestSessionLocal() as db:
        group = await create_group(db, "Detail", "d", uuid.uuid4())
        result = await get_group(group.id, db=db, _=_mock_user())
        assert result.name == "Detail"
        assert result.members == []


@pytest.mark.asyncio
async def test_groups_get_not_found():
    from app.routers.groups import get_group

    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await get_group(uuid.uuid4(), db=db, _=_mock_user())
        assert exc.value.status_code == 404


# --- groups router: update_group ---


@pytest.mark.asyncio
async def test_groups_update_endpoint():
    from app.routers.groups import update_group_endpoint
    from app.schemas.group import GroupUpdateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Old", "desc", admin.id)
        body = GroupUpdateRequest(name="New", description="updated")
        result = await update_group_endpoint(group.id, body, db=db, current_user=admin)
        assert result.name == "New"
        assert result.description == "updated"


@pytest.mark.asyncio
async def test_groups_update_not_found():
    from app.routers.groups import update_group_endpoint
    from app.schemas.group import GroupUpdateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = GroupUpdateRequest(name="Nope")
        with pytest.raises(HTTPException) as exc:
            await update_group_endpoint(uuid.uuid4(), body, db=db, current_user=admin)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_groups_update_duplicate_name():
    from app.routers.groups import update_group_endpoint
    from app.schemas.group import GroupUpdateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        await create_group(db, "Taken", "d", admin.id)
        g2 = await create_group(db, "Other", "d", admin.id)
        body = GroupUpdateRequest(name="Taken")
        with pytest.raises(HTTPException) as exc:
            await update_group_endpoint(g2.id, body, db=db, current_user=admin)
        assert exc.value.status_code == 409


# --- groups router: delete_group ---


@pytest.mark.asyncio
async def test_groups_delete_endpoint():
    from app.routers.groups import delete_group_endpoint

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "ToDelete", "d", admin.id)
        result = await delete_group_endpoint(group.id, db=db, current_user=admin)
        assert result is None


@pytest.mark.asyncio
async def test_groups_delete_not_found():
    from app.routers.groups import delete_group_endpoint

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await delete_group_endpoint(uuid.uuid4(), db=db, current_user=admin)
        assert exc.value.status_code == 404


# --- groups router: add_member ---


@pytest.mark.asyncio
async def test_groups_add_member_endpoint():
    from app.routers.groups import add_member_endpoint
    from app.schemas.group import AddMemberRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Team", "d", admin.id)
        user = await create_user(db, "m@test.com", "member", "password123")
        body = AddMemberRequest(user_id=user.id)
        result = await add_member_endpoint(group.id, body, db=db, current_user=admin)
        assert result.user_id == user.id
        assert result.username == "member"


@pytest.mark.asyncio
async def test_groups_add_member_group_not_found():
    from app.routers.groups import add_member_endpoint
    from app.schemas.group import AddMemberRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = AddMemberRequest(user_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await add_member_endpoint(uuid.uuid4(), body, db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert "Group" in exc.value.detail


@pytest.mark.asyncio
async def test_groups_add_member_user_not_found():
    from app.routers.groups import add_member_endpoint
    from app.schemas.group import AddMemberRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "NoUser", "d", admin.id)
        body = AddMemberRequest(user_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await add_member_endpoint(group.id, body, db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert "User" in exc.value.detail


@pytest.mark.asyncio
async def test_groups_add_member_duplicate():
    from app.routers.groups import add_member_endpoint
    from app.schemas.group import AddMemberRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "DupMem", "d", admin.id)
        user = await create_user(db, "dm@test.com", "dupmem", "password123")
        body = AddMemberRequest(user_id=user.id)
        await add_member_endpoint(group.id, body, db=db, current_user=admin)
        with pytest.raises(HTTPException) as exc:
            await add_member_endpoint(group.id, body, db=db, current_user=admin)
        assert exc.value.status_code == 409


# --- groups router: remove_member ---


@pytest.mark.asyncio
async def test_groups_remove_member_endpoint():
    from app.routers.groups import remove_member_endpoint

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "RemTeam", "d", admin.id)
        user = await create_user(db, "rm@test.com", "remuser", "password123")
        await add_member(db, group.id, user.id)
        result = await remove_member_endpoint(group.id, user.id, db=db, current_user=admin)
        assert result is None


@pytest.mark.asyncio
async def test_groups_remove_member_group_not_found():
    from app.routers.groups import remove_member_endpoint

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await remove_member_endpoint(uuid.uuid4(), uuid.uuid4(), db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert "Group" in exc.value.detail


@pytest.mark.asyncio
async def test_groups_remove_member_not_found():
    from app.routers.groups import remove_member_endpoint

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "NoMem", "d", admin.id)
        with pytest.raises(HTTPException) as exc:
            await remove_member_endpoint(group.id, uuid.uuid4(), db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert "Member" in exc.value.detail


# --- groups router: bulk_add_members ---


@pytest.mark.asyncio
async def test_groups_bulk_add_endpoint():
    from app.routers.groups import bulk_add_members_endpoint
    from app.schemas.group import BulkAddMembersRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Bulk", "d", admin.id)
        u1 = await create_user(db, "b1@test.com", "bulk1", "password123")
        u2 = await create_user(db, "b2@test.com", "bulk2", "password123")
        body = BulkAddMembersRequest(user_ids=[u1.id, u2.id])
        result = await bulk_add_members_endpoint(group.id, body, db=db, current_user=admin)
        assert result.added == 2
        assert result.skipped == 0


@pytest.mark.asyncio
async def test_groups_bulk_add_group_not_found():
    from app.routers.groups import bulk_add_members_endpoint
    from app.schemas.group import BulkAddMembersRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = BulkAddMembersRequest(user_ids=[uuid.uuid4()])
        with pytest.raises(HTTPException) as exc:
            await bulk_add_members_endpoint(uuid.uuid4(), body, db=db, current_user=admin)
        assert exc.value.status_code == 404


# --- groups router: bulk_remove_members ---


@pytest.mark.asyncio
async def test_groups_bulk_remove_endpoint():
    from app.routers.groups import bulk_remove_members_endpoint
    from app.schemas.group import BulkRemoveMembersRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "BulkRem", "d", admin.id)
        u1 = await create_user(db, "br1@test.com", "bulkrem1", "password123")
        await add_member(db, group.id, u1.id)
        body = BulkRemoveMembersRequest(user_ids=[u1.id])
        result = await bulk_remove_members_endpoint(group.id, body, db=db, current_user=admin)
        assert result.removed == 1
        assert result.not_found == 0


@pytest.mark.asyncio
async def test_groups_bulk_remove_group_not_found():
    from app.routers.groups import bulk_remove_members_endpoint
    from app.schemas.group import BulkRemoveMembersRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = BulkRemoveMembersRequest(user_ids=[uuid.uuid4()])
        with pytest.raises(HTTPException) as exc:
            await bulk_remove_members_endpoint(uuid.uuid4(), body, db=db, current_user=admin)
        assert exc.value.status_code == 404


# --- groups router: get_user_groups ---


@pytest.mark.asyncio
async def test_groups_get_user_groups_endpoint():
    from app.routers.groups import get_user_groups_endpoint

    async with TestSessionLocal() as db:
        user = await create_user(db, "ug@test.com", "uguser", "password123")
        g1 = await create_group(db, "UG1", "d", uuid.uuid4())
        g2 = await create_group(db, "UG2", "d", uuid.uuid4())
        await add_member(db, g1.id, user.id)
        await add_member(db, g2.id, user.id)
        result = await get_user_groups_endpoint(user.id, db=db, _=_mock_user())
        assert len(result) == 2
        names = {g.name for g in result}
        assert "UG1" in names
        assert "UG2" in names


@pytest.mark.asyncio
async def test_groups_get_user_groups_empty():
    from app.routers.groups import get_user_groups_endpoint

    async with TestSessionLocal() as db:
        result = await get_user_groups_endpoint(uuid.uuid4(), db=db, _=_mock_user())
        assert result == []


# --- dependencies/auth.py: get_current_user ---


@pytest.mark.asyncio
async def test_get_current_user_valid_token():
    """get_current_user resolves a valid JWT to the correct user."""

    from app.dependencies.auth import get_current_user
    from app.utils.jwt import create_access_token
    from fastapi.security import HTTPAuthorizationCredentials

    async with TestSessionLocal() as db:
        user = await create_user(db, "dep@test.com", "depuser", "password123")
        token = create_access_token(
            {
                "sub": str(user.id),
                "username": user.username,
                "email": user.email,
                "role": user.role.value,
            }
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_current_user(credentials=creds, db=db)
        assert result.id == user.id
        assert result.username == "depuser"


@pytest.mark.asyncio
async def test_get_current_user_invalid_token():
    """get_current_user raises 401 for an invalid JWT."""
    from app.dependencies.auth import get_current_user
    from fastapi.security import HTTPAuthorizationCredentials

    async with TestSessionLocal() as db:
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid-jwt")
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=db)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_nonexistent_user():
    """get_current_user raises 401 when JWT sub points to missing user."""
    from app.dependencies.auth import get_current_user
    from app.utils.jwt import create_access_token
    from fastapi.security import HTTPAuthorizationCredentials

    token = create_access_token(
        {
            "sub": str(uuid.uuid4()),
            "username": "ghost",
            "email": "ghost@test.com",
            "role": "user",
        }
    )
    async with TestSessionLocal() as db:
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=db)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_inactive_user():
    """get_current_user raises 401 when user is inactive."""
    from app.dependencies.auth import get_current_user
    from app.utils.jwt import create_access_token
    from fastapi.security import HTTPAuthorizationCredentials
    from sqlalchemy import update as sa_update

    async with TestSessionLocal() as db:
        user = await create_user(db, "inactive@test.com", "inactiveuser", "password123")
        await db.execute(sa_update(User).where(User.id == user.id).values(is_active=False))
        await db.commit()
        token = create_access_token(
            {
                "sub": str(user.id),
                "username": user.username,
                "email": user.email,
                "role": user.role.value,
            }
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=db)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_no_sub_in_token():
    """get_current_user raises 401 when JWT has no 'sub' claim."""
    from app.dependencies.auth import get_current_user
    from app.utils.jwt import create_access_token
    from fastapi.security import HTTPAuthorizationCredentials

    token = create_access_token({"username": "nosub", "email": "x@test.com", "role": "user"})
    async with TestSessionLocal() as db:
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=db)
        assert exc.value.status_code == 401


# --- database.py: get_db ---


@pytest.mark.asyncio
async def test_get_db_yields_session():
    """get_db is an async generator that yields a session."""

    gen = get_db()
    session = await gen.__anext__()
    assert session is not None
    # Cleanup
    try:
        await gen.__anext__()
    except StopAsyncIteration:
        pass
