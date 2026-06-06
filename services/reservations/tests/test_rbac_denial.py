"""RBAC denial sweep: require_admin endpoints in the reservations service
(admin-gated reporting endpoints) return 403 for a non-admin caller.
"""

import uuid

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.routers.reservations import bearer_scheme
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


def _override_user():
    return {"sub": str(uuid.uuid4()), "username": "regular", "role": "user"}


def _override_bearer():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-token")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[bearer_scheme] = _override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


ADMIN_ENDPOINTS = [
    ("GET", "/reports/utilization?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"),
    ("GET", "/reports/utilization.csv?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"),
]


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
@pytest.mark.asyncio
async def test_non_admin_denied(user_client, method, path):
    resp = await user_client.request(method, path)
    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)
