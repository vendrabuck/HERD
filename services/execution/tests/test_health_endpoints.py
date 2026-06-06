"""Tests for the device-health read endpoints in app.routers.health.

Exercises:
- GET /device-health/{device_id} returns a synthesized UNKNOWN row on miss
- GET /device-health/{device_id} returns the persisted row when one exists
- GET /device-health (list) is admin-only
- Pagination + last_status filter on the list endpoint
"""

import uuid
from datetime import datetime, timezone

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.device_health_status import DeviceHealthStatus
from app.routers.health import get_current_user_payload, require_admin
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def admin_payload():
    return {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}


def user_payload():
    return {"sub": str(uuid.uuid4()), "username": "user", "role": "user"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = admin_payload
    app.dependency_overrides[require_admin] = admin_payload
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = user_payload
    # Deliberately do NOT override require_admin: a real user-role caller
    # must be rejected by the admin guard.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_row(
    device_id: uuid.UUID,
    *,
    last_status: str = "HEALTHY",
    consecutive_failures: int = 0,
) -> None:
    async with TestSessionLocal() as db:
        db.add(
            DeviceHealthStatus(
                device_id=device_id,
                last_status=last_status,
                consecutive_failures=consecutive_failures,
                last_polled_at=datetime.now(timezone.utc),
                next_poll_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


# --- GET /device-health/{device_id} ---


@pytest.mark.asyncio
async def test_get_health_unknown_device_returns_synthetic_200(admin_client):
    """Polling has not run yet (or device is unpolled): return UNKNOWN, not 404."""
    device_id = uuid.uuid4()
    resp = await admin_client.get(f"/device-health/{device_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == str(device_id)
    assert body["last_status"] == "UNKNOWN"
    assert body["last_polled_at"] is None
    assert body["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_get_health_returns_persisted_row(admin_client):
    device_id = uuid.uuid4()
    await _seed_row(device_id, last_status="DEGRADED", consecutive_failures=2)
    resp = await admin_client.get(f"/device-health/{device_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_status"] == "DEGRADED"
    assert body["consecutive_failures"] == 2
    assert body["last_polled_at"] is not None


@pytest.mark.asyncio
async def test_get_health_available_to_non_admin(user_client):
    """Any authenticated user can read individual health snapshots."""
    device_id = uuid.uuid4()
    await _seed_row(device_id)
    resp = await user_client.get(f"/device-health/{device_id}")
    assert resp.status_code == 200


# --- GET /device-health (list) ---


@pytest.mark.asyncio
async def test_list_health_admin_only(user_client):
    """A non-admin user hitting the list endpoint must be rejected."""
    resp = await user_client.get("/device-health")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_health_pagination_and_filter(admin_client):
    for status in ("HEALTHY", "HEALTHY", "DEGRADED", "UNREACHABLE"):
        await _seed_row(uuid.uuid4(), last_status=status)

    # No filter: total 4
    resp = await admin_client.get("/device-health")
    assert resp.status_code == 200
    assert resp.json()["total"] == 4

    # Filter HEALTHY: total 2
    resp = await admin_client.get("/device-health?last_status=HEALTHY")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert all(item["last_status"] == "HEALTHY" for item in body["items"])

    # Pagination cap: limit=1, total still 4 but items length 1
    resp = await admin_client.get("/device-health?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert len(body["items"]) == 1


@pytest.mark.asyncio
async def test_list_health_invalid_status_filter_returns_422(admin_client):
    await _seed_row(uuid.uuid4())
    resp = await admin_client.get("/device-health?last_status=NOPE")
    assert resp.status_code == 422
