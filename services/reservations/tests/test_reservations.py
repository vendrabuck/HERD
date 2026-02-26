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


async def _create_test_reservation(client, device_ids=None, **overrides):
    """Helper: creates a reservation with mocked external calls."""
    if device_ids is None:
        device_ids = [DEVICE_A]
    topo = overrides.pop("topology_type", "PHYSICAL")
    devices = [make_device_response(did, topo) for did in device_ids]
    body = {
        "device_ids": device_ids,
        "purpose": "Test lab setup",
        "start_time": START,
        "end_time": END,
        **overrides,
    }
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=devices),
    ), patch(
        "app.services.reservation_service._publish_nats_event",
        new=AsyncMock(),
    ), patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.post("/", json=body)
    return resp


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
    ), patch(
        "app.services.reservation_service._update_device_statuses",
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
    ), patch(
        "app.services.reservation_service._update_device_statuses",
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
    ), patch(
        "app.services.reservation_service._update_device_statuses",
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
    ), patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        create_resp = await client.post(
            "/",
            json={"device_ids": [DEVICE_A], "start_time": START, "end_time": END},
        )
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        del_resp = await client.delete(f"/{reservation_id}")
    assert del_resp.status_code == 204


# --- GET single reservation ---


@pytest.mark.asyncio
async def test_get_reservation(client):
    create_resp = await _create_test_reservation(client, [DEVICE_A, DEVICE_B])
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    resp = await client.get(f"/{reservation_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == reservation_id
    assert resp.json()["purpose"] == "Test lab setup"


@pytest.mark.asyncio
async def test_get_reservation_not_found(client):
    resp = await client.get(f"/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- Cancel edge cases ---


@pytest.mark.asyncio
async def test_cancel_reservation_not_found(client):
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.delete(f"/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_already_cancelled(client):
    create_resp = await _create_test_reservation(client)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        first = await client.delete(f"/{reservation_id}")
        assert first.status_code == 204
        second = await client.delete(f"/{reservation_id}")
        assert second.status_code == 204


# --- Release tests ---


@pytest.mark.asyncio
async def test_release_reservation(client):
    create_resp = await _create_test_reservation(client)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.put(f"/{reservation_id}/release")
    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_release_reservation_not_found(client):
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.put(f"/{uuid.uuid4()}/release")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_release_non_active_reservation(client):
    """Cancel, then try to release; should return reservation unchanged (still CANCELLED)."""
    create_resp = await _create_test_reservation(client)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        await client.delete(f"/{reservation_id}")
        resp = await client.put(f"/{reservation_id}/release")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


# --- Device not available ---


@pytest.mark.asyncio
async def test_device_not_available(client):
    """Reservation creation should fail if a device is not AVAILABLE."""
    reserved_device = make_device_response(DEVICE_A, "PHYSICAL")
    reserved_device["status"] = "RESERVED"
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[reserved_device]),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 422
    assert "not available" in resp.json()["detail"].lower()


# --- Validation tests ---


@pytest.mark.asyncio
async def test_create_reservation_empty_device_ids(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": [],
            "purpose": "Test",
            "start_time": START,
            "end_time": END,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_end_before_start(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": [DEVICE_A],
            "purpose": "Test",
            "start_time": END,
            "end_time": START,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_end_equals_start(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": [DEVICE_A],
            "purpose": "Test",
            "start_time": START,
            "end_time": START,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_invalid_device_id(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": ["not-a-uuid"],
            "purpose": "Test",
            "start_time": START,
            "end_time": END,
        },
    )
    assert resp.status_code == 422


# --- Conflict edge cases ---


@pytest.mark.asyncio
async def test_adjacent_reservations_allowed(client):
    """Reservation B starts exactly when A ends; no conflict (half-open interval)."""
    a_start = START
    a_end = END
    resp_a = await _create_test_reservation(client, [DEVICE_A], start_time=a_start, end_time=a_end)
    assert resp_a.status_code == 201
    # B starts exactly at A's end
    b_start = a_end
    b_end = (NOW + timedelta(hours=5)).isoformat()
    resp_b = await _create_test_reservation(client, [DEVICE_A], start_time=b_start, end_time=b_end)
    assert resp_b.status_code == 201


@pytest.mark.asyncio
async def test_no_conflict_with_cancelled_reservation(client):
    """Cancel reservation A, then rebook same device and window; expect 201."""
    resp_a = await _create_test_reservation(client, [DEVICE_A])
    assert resp_a.status_code == 201
    reservation_id = resp_a.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        await client.delete(f"/{reservation_id}")
    resp_b = await _create_test_reservation(client, [DEVICE_A])
    assert resp_b.status_code == 201


@pytest.mark.asyncio
async def test_conflict_then_cancel_then_rebook(client):
    """Create A, attempt overlapping B (409), cancel A, create B again (201)."""
    resp_a = await _create_test_reservation(client, [DEVICE_A])
    assert resp_a.status_code == 201
    reservation_id = resp_a.json()["id"]
    # Overlapping B should conflict
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ):
        resp_conflict = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Overlapping",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp_conflict.status_code == 409
    # Cancel A
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        await client.delete(f"/{reservation_id}")
    # Now B should succeed
    resp_b = await _create_test_reservation(client, [DEVICE_A])
    assert resp_b.status_code == 201
