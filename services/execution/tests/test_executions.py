import uuid

import pytest
from app.database import Base, get_db
from app.main import app
from app.routers.executions import _require_internal_token, get_current_user_payload, require_admin
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


def _override_admin():
    return ADMIN_PAYLOAD


def _override_internal_token():
    return None


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def internal_client():
    app.dependency_overrides[_require_internal_token] = _override_internal_token
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Health ---


@pytest.mark.asyncio
async def test_health(admin_client):
    resp = await admin_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "execution"


# --- Execution runs CRUD ---


@pytest.mark.asyncio
async def test_list_runs_empty(admin_client):
    resp = await admin_client.get("/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_get_run_not_found(admin_client):
    fake_id = str(uuid.uuid4())
    resp = await admin_client.get(f"/runs/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_run_not_found(admin_client):
    fake_id = str(uuid.uuid4())
    resp = await admin_client.post(f"/runs/{fake_id}/retry")
    assert resp.status_code == 404


# --- Unauthenticated access ---


@pytest.fixture
async def unauthenticated_client():
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unauthenticated_list_runs(unauthenticated_client):
    resp = await unauthenticated_client.get("/runs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_get_run(unauthenticated_client):
    fake_id = str(uuid.uuid4())
    resp = await unauthenticated_client.get(f"/runs/{fake_id}")
    assert resp.status_code == 401


# --- Manual execute requires admin ---


@pytest.mark.asyncio
async def test_manual_execute_unauthenticated(unauthenticated_client):
    resp = await unauthenticated_client.post(
        "/execute",
        json={
            "device_id": str(uuid.uuid4()),
            "action": "status",
            "user_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 401
