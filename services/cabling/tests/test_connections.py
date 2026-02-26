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


# --- Edge cases ---


@pytest.mark.asyncio
async def test_list_connections_empty(admin_client):
    """GET /connections with none created returns 200 + empty list."""
    resp = await admin_client.get("/connections")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_connection_self_loop(admin_client):
    """device_a_id == device_b_id; documents current behavior (201 accepted)."""
    device_id = str(uuid.uuid4())
    body = {
        "device_a_id": device_id,
        "port_a": "eth0",
        "device_b_id": device_id,
        "port_b": "eth1",
        "connection_type": "ethernet",
        "notes": "self loop",
    }
    resp = await admin_client.post("/connections", json=body)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_connection_full_lifecycle(admin_client):
    """Create, list (verify present), get by id, delete, get (verify 404)."""
    body = _connection_body()
    create_resp = await admin_client.post("/connections", json=body)
    assert create_resp.status_code == 201
    conn_id = create_resp.json()["id"]
    # List
    list_resp = await admin_client.get("/connections")
    assert list_resp.status_code == 200
    assert any(c["id"] == conn_id for c in list_resp.json())
    # Get by id
    get_resp = await admin_client.get(f"/connections/{conn_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == conn_id
    # Delete
    del_resp = await admin_client.delete(f"/connections/{conn_id}")
    assert del_resp.status_code == 204
    # Get to verify 404
    gone_resp = await admin_client.get(f"/connections/{conn_id}")
    assert gone_resp.status_code == 404
