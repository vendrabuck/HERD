"""RBAC denial sweep: require_admin endpoints in the cabling service
(connection create/delete) return 403 for a non-admin caller.
"""

import uuid

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


CONNECTION_ID = str(uuid.uuid4())
CONNECTION_BODY = {
    "device_a_id": str(uuid.uuid4()),
    "port_a": "eth0",
    "device_b_id": str(uuid.uuid4()),
    "port_b": "eth1",
    "connection_type": "ethernet",
}

ADMIN_ENDPOINTS: list[tuple[str, str, dict | None]] = [
    ("POST", "/connections", CONNECTION_BODY),
    ("DELETE", f"/connections/{CONNECTION_ID}", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
@pytest.mark.asyncio
async def test_non_admin_denied(user_client, method, path, body):
    if body is None:
        resp = await user_client.request(method, path)
    else:
        resp = await user_client.request(method, path, json=body)
    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)
