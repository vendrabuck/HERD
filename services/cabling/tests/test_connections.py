import uuid

import pytest
from app.dependencies import get_current_user_payload, require_admin
from app.main import _connections, app
from httpx import ASGITransport, AsyncClient

ADMIN_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": str(uuid.uuid4()), "username": "viewer", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


@pytest.fixture(autouse=True)
def clear_connections():
    _connections.clear()
    yield
    _connections.clear()


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client_no_admin():
    """Client where require_admin is NOT overridden, so user role gets 403."""
    app.dependency_overrides[get_current_user_payload] = _override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _connection_body():
    return {
        "device_a_id": str(uuid.uuid4()),
        "port_a": "eth0",
        "device_b_id": str(uuid.uuid4()),
        "port_b": "eth1",
        "connection_type": "ethernet",
        "notes": "test link",
    }


@pytest.mark.asyncio
async def test_create_connection(admin_client):
    resp = await admin_client.post("/connections", json=_connection_body())
    assert resp.status_code == 201
    data = resp.json()
    assert data["port_a"] == "eth0"
    assert data["created_by"] == "admin"


@pytest.mark.asyncio
async def test_list_connections(admin_client):
    await admin_client.post("/connections", json=_connection_body())
    await admin_client.post("/connections", json=_connection_body())
    resp = await admin_client.get("/connections")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_get_connection(admin_client):
    create_resp = await admin_client.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    resp = await admin_client.get(f"/connections/{conn_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conn_id


@pytest.mark.asyncio
async def test_get_connection_not_found(admin_client):
    resp = await admin_client.get("/connections/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_connection(admin_client):
    create_resp = await admin_client.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    resp = await admin_client.delete(f"/connections/{conn_id}")
    assert resp.status_code == 204
    # Verify it is gone
    resp = await admin_client.get(f"/connections/{conn_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_connection_not_found(admin_client):
    resp = await admin_client.delete("/connections/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_can_list(user_client):
    resp = await user_client.get("/connections")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_cannot_create(user_client_no_admin):
    resp = await user_client_no_admin.post("/connections", json=_connection_body())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete(user_client_no_admin):
    # Manually add a connection so we can try to delete it
    conn_id = str(uuid.uuid4())
    _connections[conn_id] = {"id": conn_id}
    resp = await user_client_no_admin.delete(f"/connections/{conn_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_can_get_connection(admin_client, user_client):
    create_resp = await admin_client.post("/connections", json=_connection_body())
    conn_id = create_resp.json()["id"]
    resp = await user_client.get(f"/connections/{conn_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conn_id


@pytest.mark.asyncio
async def test_create_connection_missing_fields(admin_client):
    resp = await admin_client.post("/connections", json={"device_a_id": str(uuid.uuid4())})
    assert resp.status_code == 422
