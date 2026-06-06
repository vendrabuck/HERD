"""Tests for the internal-token-guarded endpoints on the auth service.

iter 2 of ROADMAP #13 (health alerting) introduces the first
internal-token endpoint on auth: GET /internal/admins returns user IDs
of admin and superadmin users so notifications can fan out
admin-targeted events without a real admin JWT.
"""

import uuid

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.user import Role, User
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

INTERNAL_TOKEN = "test-internal-token-iter2"  # noqa: S105

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


@pytest.fixture
async def internal_client(monkeypatch):
    monkeypatch.setattr("app.routers.internal.settings.internal_api_token", INTERNAL_TOKEN)
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_user(
    *, role: Role, is_active: bool = True, username: str | None = None
) -> uuid.UUID:
    uid = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            User(
                id=uid,
                email=f"{uid}@example.com",
                username=username or f"u-{uid}",
                hashed_password="x",
                is_active=is_active,
                role=role,
            )
        )
        await session.commit()
    return uid


@pytest.mark.asyncio
async def test_internal_admins_returns_admin_and_superadmin(internal_client):
    user_id = await _seed_user(role=Role.USER)  # noqa: F841 - seeded to verify exclusion
    admin_id = await _seed_user(role=Role.ADMIN)
    superadmin_id = await _seed_user(role=Role.SUPERADMIN)

    resp = await internal_client.get(
        "/internal/admins", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    ids = {uuid.UUID(s) for s in resp.json()}
    assert admin_id in ids
    assert superadmin_id in ids
    assert user_id not in ids
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_internal_admins_excludes_inactive_users(internal_client):
    active_admin = await _seed_user(role=Role.ADMIN, is_active=True)
    inactive_admin = await _seed_user(role=Role.ADMIN, is_active=False)  # noqa: F841

    resp = await internal_client.get(
        "/internal/admins", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    ids = {uuid.UUID(s) for s in resp.json()}
    assert active_admin in ids
    assert inactive_admin not in ids


@pytest.mark.asyncio
async def test_internal_admins_empty_when_no_admins_exist(internal_client):
    await _seed_user(role=Role.USER)
    resp = await internal_client.get(
        "/internal/admins", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_internal_admins_requires_valid_token(internal_client):
    await _seed_user(role=Role.ADMIN)

    resp = await internal_client.get(
        "/internal/admins", headers={"X-Internal-Token": "wrong-token"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_admins_requires_token_header(internal_client):
    await _seed_user(role=Role.ADMIN)
    resp = await internal_client.get("/internal/admins")
    # Header is required (Header(...)); FastAPI returns 422 when missing
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_user_contact_returns_email_and_username(internal_client):
    uid = await _seed_user(role=Role.USER, username="alice")
    resp = await internal_client.get(
        f"/internal/users/{uid}/contact", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(uid)
    assert body["email"] == f"{uid}@example.com"
    assert body["username"] == "alice"


@pytest.mark.asyncio
async def test_internal_user_contact_404_for_unknown_user(internal_client):
    resp = await internal_client.get(
        f"/internal/users/{uuid.uuid4()}/contact",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_internal_user_contact_requires_valid_token(internal_client):
    uid = await _seed_user(role=Role.USER)
    resp = await internal_client.get(
        f"/internal/users/{uid}/contact", headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_admins_503_when_token_not_configured(monkeypatch):
    """If the deployer has not set INTERNAL_API_TOKEN, the endpoint is
    deliberately unreachable rather than silently allowing any caller in.
    """
    monkeypatch.setattr("app.routers.internal.settings.internal_api_token", "")
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/internal/admins", headers={"X-Internal-Token": "anything"})
    app.dependency_overrides.clear()
    assert resp.status_code == 503
