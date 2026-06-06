"""Tests for the LDAP auth branch in authenticate_user + /register gating.

Ldap3 bind is stubbed; these tests exercise the branching and JIT user
creation in app.services.auth_service, and the /register short-circuit in
app.routers.auth.
"""

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services import auth_service, ldap_service
from app.services.auth_service import authenticate_user, create_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def ldap_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)


@pytest.fixture
def fake_bind(monkeypatch):
    async def _bind(username, password):
        if username == "alice" and password == "good":
            return ldap_service.LdapIdentity(
                username="alice", email="alice@example.com", dn="cn=alice"
            )
        return None

    monkeypatch.setattr(ldap_service, "bind_user", _bind)


@pytest.mark.asyncio
async def test_register_blocked_when_ldap_mode(client, ldap_mode):
    resp = await client.post(
        "/register",
        json={"email": "new@example.com", "username": "new", "password": "password123"},
    )
    assert resp.status_code == 409
    assert "ldap" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_ldap_login_jit_provisions_user(client, ldap_mode, fake_bind):
    resp = await client.post("/login", json={"email": "alice", "password": "good"})
    assert resp.status_code == 200
    async with TestSessionLocal() as db:
        row = (await db.execute(select(User).where(User.email == "alice@example.com"))).scalar_one()
        assert row.auth_source == "ldap"
        assert row.hashed_password is None


@pytest.mark.asyncio
async def test_ldap_login_reuses_existing_ldap_user(client, ldap_mode, fake_bind):
    # First login provisions the user.
    await client.post("/login", json={"email": "alice", "password": "good"})
    # Second login must succeed without creating a duplicate.
    resp = await client.post("/login", json={"email": "alice", "password": "good"})
    assert resp.status_code == 200
    async with TestSessionLocal() as db:
        rows = (
            (await db.execute(select(User).where(User.email == "alice@example.com")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_ldap_mode_rejects_existing_local_account(client, ldap_mode, fake_bind):
    # Pre-existing local user with the same email that LDAP would return.
    async with TestSessionLocal() as db:
        await create_user(db, "alice@example.com", "alice-local", "localpw123")
    resp = await client.post("/login", json={"email": "alice", "password": "good"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ldap_login_bad_password(client, ldap_mode, fake_bind):
    resp = await client.post("/login", json={"email": "alice", "password": "bad"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_local_mode_blocks_ldap_user(monkeypatch):
    # Default auth_method is local; an LDAP-sourced User must not authenticate
    # against the local path even if someone happens to know a bcrypt-hash
    # collision.
    async with TestSessionLocal() as db:
        user = User(
            email="ldapuser@example.com",
            username="ldapuser",
            hashed_password=None,
            auth_source="ldap",
        )
        db.add(user)
        await db.commit()
        result = await authenticate_user(db, "ldapuser@example.com", "anything")
    assert result is None


def test_auth_service_exposes_create_ldap_user():
    # Sanity check that the helper is importable from the service module.
    assert callable(auth_service.create_ldap_user)
