"""Tests for the internal-token-guarded POST /internal/check (issue #704).

Fire-time authority re-check: the inventory apply-job scheduler has only a
job's created_by user_id, not a bearer token, so it cannot call the
JWT-guarded POST /check. This route runs the same grant evaluation with
group membership resolved through auth's internal-token-guarded route
instead of the forwarded JWT.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.grant import ResourceGrant
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

INTERNAL_TOKEN = "test-internal-token-704"  # noqa: S105

_user_id = uuid.uuid4()
_group_id = uuid.uuid4()
_device_id = uuid.uuid4()


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


async def _seed_grant(permission: str = "manage") -> None:
    async with TestSessionLocal() as session:
        session.add(
            ResourceGrant(
                group_id=_group_id,
                resource_type="device",
                resource_id=_device_id,
                permission=permission,
                granted_by=None,
            )
        )
        await session.commit()


def _body(permission: str = "manage") -> dict:
    return {
        "user_id": str(_user_id),
        "resource_type": "device",
        "resource_id": str(_device_id),
        "permission": permission,
    }


@pytest.fixture
async def internal_client(monkeypatch):
    monkeypatch.setattr("app.routers.internal.settings.internal_api_token", INTERNAL_TOKEN)
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("app.routers.internal.fetch_user_groups_internal", new_callable=AsyncMock)
async def test_internal_check_allowed(mock_fetch, internal_client):
    mock_fetch.return_value = [_group_id]
    await _seed_grant(permission="manage")
    resp = await internal_client.post(
        "/internal/check", json=_body(), headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["allowed"] is True
    assert len(data["grants"]) == 1


@pytest.mark.asyncio
@patch("app.routers.internal.fetch_user_groups_internal", new_callable=AsyncMock)
async def test_internal_check_denied_no_grant(mock_fetch, internal_client):
    mock_fetch.return_value = [_group_id]
    resp = await internal_client.post(
        "/internal/check", json=_body(), headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
@patch("app.routers.internal.fetch_user_groups_internal", new_callable=AsyncMock)
async def test_internal_check_auth_unreachable_denies(mock_fetch, internal_client):
    """fetch_user_groups_internal is closed-by-default (empty list on any
    transport failure or non-200), so an unreachable auth service denies
    here without a special-cased branch in this route."""
    mock_fetch.return_value = []
    await _seed_grant(permission="manage")
    resp = await internal_client.post(
        "/internal/check", json=_body(), headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["allowed"] is False


@pytest.mark.asyncio
async def test_internal_check_requires_valid_token(internal_client):
    resp = await internal_client.post(
        "/internal/check", json=_body(), headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_check_requires_token_header(internal_client):
    resp = await internal_client.post("/internal/check", json=_body())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_check_503_when_token_not_configured(monkeypatch):
    monkeypatch.setattr("app.routers.internal.settings.internal_api_token", "")
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/internal/check", json=_body(), headers={"X-Internal-Token": "anything"}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 503
