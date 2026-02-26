import uuid

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "test-admin-id", "username": "testadmin", "role": "admin"}


def override_auth_user():
    return {"sub": "test-user-id", "username": "testuser", "role": "user"}


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
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


DEVICE_PAYLOAD = {
    "name": "FW-01",
    "device_type": "FIREWALL",
    "topology_type": "PHYSICAL",
    "status": "AVAILABLE",
    "location": "Rack A, U1",
    "specs": {"model": "PA-3020", "interfaces": 8},
}


@pytest.mark.asyncio
async def test_create_device(client):
    resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "FW-01"
    assert data["device_type"] == "FIREWALL"
    assert data["topology_type"] == "PHYSICAL"


@pytest.mark.asyncio
async def test_list_devices(client):
    await client.post("/devices", json=DEVICE_PAYLOAD)
    resp = await client.get("/devices")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_get_device(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    resp = await client.get(f"/devices/{device_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == device_id


@pytest.mark.asyncio
async def test_update_device(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    resp = await client.put(f"/devices/{device_id}", json={"status": "OFFLINE"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OFFLINE"


@pytest.mark.asyncio
async def test_delete_device(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    resp = await client.delete(f"/devices/{device_id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/devices/{device_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_filter_by_topology_type(client):
    await client.post("/devices", json={**DEVICE_PAYLOAD, "topology_type": "PHYSICAL"})
    await client.post(
        "/devices",
        json={**DEVICE_PAYLOAD, "name": "Cloud-FW-01", "topology_type": "CLOUD"},
    )
    resp = await client.get("/devices?topology_type=PHYSICAL")
    assert resp.status_code == 200
    assert all(d["topology_type"] == "PHYSICAL" for d in resp.json())


@pytest.mark.asyncio
async def test_user_cannot_create_device(user_client):
    resp = await user_client.post("/devices", json=DEVICE_PAYLOAD)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_update_device(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.put(f"/devices/{device_id}", json={"status": "OFFLINE"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete_device(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.delete(f"/devices/{device_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_can_list_devices(user_client):
    resp = await user_client.get("/devices")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_internal_status_update(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RESERVED"


@pytest.mark.asyncio
async def test_internal_status_update_bad_token(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403


# --- 404 tests ---


@pytest.mark.asyncio
async def test_get_device_not_found(client):
    resp = await client.get(f"/devices/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_device_not_found(client):
    resp = await client.put(f"/devices/{uuid.uuid4()}", json={"status": "OFFLINE"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_device_not_found(client):
    resp = await client.delete(f"/devices/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- Internal status missing token ---


@pytest.mark.asyncio
async def test_internal_status_update_missing_token(client):
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    device_id = create_resp.json()["id"]
    resp = await client.post(
        f"/devices/{device_id}/status",
        json={"status": "RESERVED"},
    )
    assert resp.status_code == 422


# --- Filter by status ---


@pytest.mark.asyncio
async def test_filter_by_status(client):
    await client.post("/devices", json=DEVICE_PAYLOAD)
    create2 = await client.post(
        "/devices", json={**DEVICE_PAYLOAD, "name": "FW-02", "status": "AVAILABLE"}
    )
    device2_id = create2.json()["id"]
    # Update second device to OFFLINE
    await client.put(f"/devices/{device2_id}", json={"status": "OFFLINE"})
    resp = await client.get("/devices?status=AVAILABLE")
    assert resp.status_code == 200
    devices = resp.json()
    assert all(d["status"] == "AVAILABLE" for d in devices)
    assert len(devices) == 1


# --- Query parameter tests ---


@pytest.mark.asyncio
async def test_filter_by_device_type(client):
    await client.post("/devices", json=DEVICE_PAYLOAD)  # FIREWALL
    await client.post(
        "/devices", json={**DEVICE_PAYLOAD, "name": "SW-01", "device_type": "SWITCH"}
    )
    resp = await client.get("/devices?device_type=FIREWALL")
    assert resp.status_code == 200
    devices = resp.json()
    assert len(devices) == 1
    assert devices[0]["device_type"] == "FIREWALL"


@pytest.mark.asyncio
async def test_pagination_limit(client):
    for i in range(3):
        await client.post("/devices", json={**DEVICE_PAYLOAD, "name": f"FW-{i}"})
    resp = await client.get("/devices?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_pagination_skip(client):
    for i in range(3):
        await client.post("/devices", json={**DEVICE_PAYLOAD, "name": f"FW-{i}"})
    resp = await client.get("/devices?skip=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_combined_filters(client):
    await client.post("/devices", json=DEVICE_PAYLOAD)  # FIREWALL + PHYSICAL
    await client.post(
        "/devices",
        json={
            **DEVICE_PAYLOAD, "name": "SW-01",
            "device_type": "SWITCH", "topology_type": "PHYSICAL",
        },
    )
    await client.post(
        "/devices",
        json={
            **DEVICE_PAYLOAD, "name": "Cloud-FW",
            "device_type": "FIREWALL", "topology_type": "CLOUD",
        },
    )
    resp = await client.get("/devices?device_type=FIREWALL&topology_type=PHYSICAL")
    assert resp.status_code == 200
    devices = resp.json()
    assert len(devices) == 1
    assert devices[0]["device_type"] == "FIREWALL"
    assert devices[0]["topology_type"] == "PHYSICAL"


@pytest.mark.asyncio
async def test_filter_invalid_device_type(client):
    resp = await client.get("/devices?device_type=INVALID")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_device_full_lifecycle(client):
    """Create, update name, GET (verify update), delete, GET (verify 404)."""
    create_resp = await client.post("/devices", json=DEVICE_PAYLOAD)
    assert create_resp.status_code == 201
    device_id = create_resp.json()["id"]
    # Update name
    update_resp = await client.put(f"/devices/{device_id}", json={"name": "FW-RENAMED"})
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "FW-RENAMED"
    # GET to verify update
    get_resp = await client.get(f"/devices/{device_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "FW-RENAMED"
    # Delete
    del_resp = await client.delete(f"/devices/{device_id}")
    assert del_resp.status_code == 204
    # GET to verify 404
    gone_resp = await client.get(f"/devices/{device_id}")
    assert gone_resp.status_code == 404
