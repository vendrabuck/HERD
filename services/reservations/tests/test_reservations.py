"""
Reservations service tests.
The inventory service HTTP calls are mocked with respx.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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

USER_ID = str(uuid.uuid4())
DEVICE_A = str(uuid.uuid4())
DEVICE_B = str(uuid.uuid4())
DEVICE_CLOUD = str(uuid.uuid4())

NOW = datetime.now(timezone.utc)
START = (NOW + timedelta(hours=1)).isoformat()
END = (NOW + timedelta(hours=3)).isoformat()


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth():
    return {"sub": USER_ID, "username": "testuser", "role": "user"}


def override_bearer():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-token")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def make_device_response(device_id: str, topology_type: str = "PHYSICAL") -> dict:
    return {
        "id": device_id,
        "name": f"device-{device_id[:8]}",
        "device_type": "FIREWALL",
        "topology_type": topology_type,
        "status": "AVAILABLE",
        "location": None,
        "specs": None,
        "description": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


@pytest.mark.asyncio
async def test_create_reservation(client):
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(
            return_value=[
                make_device_response(DEVICE_A, "PHYSICAL"),
                make_device_response(DEVICE_B, "PHYSICAL"),
            ]
        ),
    ), patch(
        "app.services.reservation_service._publish_nats_event",
        new=AsyncMock(),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A, DEVICE_B],
                "purpose": "Test lab setup",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["topology_type"] == "PHYSICAL"
    assert data["status"] == "ACTIVE"
    assert len(data["device_ids"]) == 2


@pytest.mark.asyncio
async def test_mixed_topology_type_rejected(client):
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(
            return_value=[
                make_device_response(DEVICE_A, "PHYSICAL"),
                make_device_response(DEVICE_CLOUD, "CLOUD"),
            ]
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A, DEVICE_CLOUD],
                "purpose": "Mixed",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 422
    assert "topology" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_conflict_detection(client):
    # First reservation succeeds
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ), patch(
        "app.services.reservation_service._publish_nats_event",
        new=AsyncMock(),
    ):
        resp1 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp1.status_code == 201

    # Second reservation for same device in overlapping window conflicts
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ):
        resp2 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_list_reservations(client):
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ), patch(
        "app.services.reservation_service._publish_nats_event",
        new=AsyncMock(),
    ):
        await client.post(
            "/",
            json={"device_ids": [DEVICE_A], "start_time": START, "end_time": END},
        )
    resp = await client.get("/")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_cancel_reservation(client):
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ), patch(
        "app.services.reservation_service._publish_nats_event",
        new=AsyncMock(),
    ):
        create_resp = await client.post(
            "/",
            json={"device_ids": [DEVICE_A], "start_time": START, "end_time": END},
        )
    reservation_id = create_resp.json()["id"]
    del_resp = await client.delete(f"/{reservation_id}")
    assert del_resp.status_code == 204
