import io
import uuid
from unittest.mock import AsyncMock, patch

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
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


def override_auth_user():
    return {"sub": "00000000-0000-0000-0000-000000000002", "username": "testuser", "role": "user"}


_mock_storage: dict[str, bytes] = {}


def mock_upload_object(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def mock_delete_object(key: str) -> None:
    _mock_storage.pop(key, None)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _mock_storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch("app.services.driver_service.upload_object", side_effect=mock_upload_object),
        patch("app.services.driver_service.delete_object", side_effect=mock_delete_object),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_reservation_guard():
    """Default the issue #391 delete guard to "no blocking reservations" so a
    device delete in this suite never reaches a real reservations service."""
    with patch(
        "app.routers.devices.find_blocking_reservations_for_device",
        new=AsyncMock(return_value=[]),
    ):
        yield


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


DEVICE_TEMPLATE_PAYLOAD = {
    "name": "Firewall",
    "template_type": "device",
    "vendor": "V",
    "model": "M",
    "sections": [
        {
            "name": "General",
            "fields": [
                {"key": "model", "label": "Model", "type": "string", "required": True},
            ],
        },
    ],
}

PASSWORD_PORT_TEMPLATE_PAYLOAD = {
    "name": "Secure Port",
    "template_type": "port",
    "sections": [
        {
            "name": "Auth",
            "fields": [
                {"key": "secret", "label": "Secret", "type": "password", "required": False},
            ],
        },
    ],
}

PORT_TEMPLATE_PAYLOAD = {
    "name": "GigE Port",
    "template_type": "port",
    "sections": [
        {
            "name": "Config",
            "fields": [
                {
                    "key": "speed",
                    "label": "Speed",
                    "type": "dropdown",
                    "required": True,
                    "options": ["1G", "10G", "25G"],
                },
                {
                    "key": "duplex",
                    "label": "Duplex",
                    "type": "dropdown",
                    "options": ["full", "half"],
                },
                {"key": "enabled", "label": "Enabled", "type": "boolean"},
                {"key": "description", "label": "Description", "type": "string"},
            ],
        },
    ],
}


_driver_counter = 0


async def _create_driver(client) -> str:
    global _driver_counter
    _driver_counter += 1
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={"name": f"Port Test Driver {_driver_counter}", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert drv_resp.status_code == 201
    return drv_resp.json()["id"]


async def _create_device_template(client) -> str:
    driver_id = await _create_driver(client)
    resp = await client.post("/templates", json={**DEVICE_TEMPLATE_PAYLOAD, "driver_id": driver_id})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_port_template(client) -> str:
    resp = await client.post("/templates", json=PORT_TEMPLATE_PAYLOAD)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_device(client, template_id: str) -> str:
    resp = await client.post(
        "/devices",
        json={
            "name": "FW-01",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "field_data": {"model": "EX3300"},
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _port_payload(port_template_id: str) -> dict:
    return {
        "name": "GigE0/1",
        "template_id": port_template_id,
        "field_data": {"speed": "1G", "duplex": "full", "enabled": True},
    }


# --- CRUD happy paths ---


@pytest.mark.asyncio
async def test_create_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "GigE0/1"
    assert data["device_id"] == dev_id
    assert data["template_id"] == pt_id
    assert data["template_name"] == "GigE Port"
    assert data["field_data"]["speed"] == "1G"


@pytest.mark.asyncio
async def test_list_ports(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "GigE0/2",
            "template_id": pt_id,
            "field_data": {"speed": "10G"},
        },
    )
    resp = await client.get(f"/devices/{dev_id}/ports")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_get_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    resp = await client.get(f"/ports/{port_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == port_id
    assert resp.json()["template_name"] == "GigE Port"


@pytest.mark.asyncio
async def test_update_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    resp = await client.put(f"/ports/{port_id}", json={"name": "GigE0/1-renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "GigE0/1-renamed"


@pytest.mark.asyncio
async def test_update_port_field_data(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    resp = await client.put(
        f"/ports/{port_id}",
        json={
            "field_data": {"speed": "10G", "duplex": "full"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["field_data"]["speed"] == "10G"


@pytest.mark.asyncio
async def test_delete_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    resp = await client.delete(f"/ports/{port_id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/ports/{port_id}")
    assert get_resp.status_code == 404


# --- Validation tests ---


@pytest.mark.asyncio
async def test_create_port_wrong_template_type(client):
    """Using a device template for a port should fail."""
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "Bad Port",
            "template_id": dt_id,
            "field_data": {},
        },
    )
    assert resp.status_code == 422
    assert "not a port template" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_port_missing_required_field(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "Bad Port",
            "template_id": pt_id,
            "field_data": {},
        },
    )
    assert resp.status_code == 422
    assert "speed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_port_invalid_field_type(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "Bad Port",
            "template_id": pt_id,
            "field_data": {"speed": "1G", "enabled": "not-a-bool"},
        },
    )
    assert resp.status_code == 422
    assert "enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_port_unknown_field(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "Bad Port",
            "template_id": pt_id,
            "field_data": {"speed": "1G", "nonexistent": "value"},
        },
    )
    assert resp.status_code == 422
    assert "nonexistent" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_port_invalid_dropdown_value(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "Bad Port",
            "template_id": pt_id,
            "field_data": {"speed": "100G"},
        },
    )
    assert resp.status_code == 422
    assert "speed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_port_device_not_found(client):
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{uuid.uuid4()}/ports",
        json={
            "name": "Port",
            "template_id": pt_id,
            "field_data": {"speed": "1G"},
        },
    )
    assert resp.status_code == 404
    assert "Device not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_port_template_not_found(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "Port",
            "template_id": str(uuid.uuid4()),
            "field_data": {},
        },
    )
    assert resp.status_code == 422
    assert "Template not found" in resp.json()["detail"]


# --- Permission tests ---


@pytest.mark.asyncio
async def test_user_lists_ports_of_visible_device(client):
    """A non-admin sees ports of a device that is visible through their groups."""
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.ports._resolve_visible_device_ids",
        new=AsyncMock(return_value={uuid.UUID(dev_id)}),
    ):
        resp = await client.get(f"/devices/{dev_id}/ports")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_user_gets_port_of_visible_device(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.ports._resolve_visible_device_ids",
        new=AsyncMock(return_value={uuid.UUID(dev_id)}),
    ):
        resp = await client.get(f"/ports/{port_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_denied_ports_of_invisible_device(client):
    """Regression (issue #310): a non-admin must not read the ports of a device
    outside their group visibility; both the list and single-port reads 404,
    never exposing the port or its field_data."""
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.ports._resolve_visible_device_ids",
        new=AsyncMock(return_value=set()),  # user can see no devices
    ):
        list_resp = await client.get(f"/devices/{dev_id}/ports")
        get_resp = await client.get(f"/ports/{port_id}")
    assert list_resp.status_code == 404
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_port_password_field_redacted_for_non_admin(client):
    """Regression (issue #310): password-typed port field values are masked for
    a non-admin read and returned raw for an admin, matching the device reads."""
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_resp = await client.post("/templates", json=PASSWORD_PORT_TEMPLATE_PAYLOAD)
    assert pt_resp.status_code == 201
    pt_id = pt_resp.json()["id"]
    create_resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={"name": "p1", "template_id": pt_id, "field_data": {"secret": "hunter2"}},
    )
    assert create_resp.status_code == 201
    port_id = create_resp.json()["id"]

    # Admin read returns the raw secret.
    admin_resp = await client.get(f"/ports/{port_id}")
    assert admin_resp.json()["field_data"]["secret"] == "hunter2"

    # Non-admin read (device visible) masks it; the cleartext never appears.
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    with patch(
        "app.routers.ports._resolve_visible_device_ids",
        new=AsyncMock(return_value={uuid.UUID(dev_id)}),
    ):
        user_resp = await client.get(f"/ports/{port_id}")
    assert user_resp.status_code == 200
    assert user_resp.json()["field_data"]["secret"] != "hunter2"
    assert "hunter2" not in str(user_resp.json())


@pytest.mark.asyncio
async def test_user_cannot_create_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_update_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.put(f"/ports/{port_id}", json={"name": "hacked"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete_port(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.delete(f"/ports/{port_id}")
    assert resp.status_code == 403


# --- 404 tests ---


@pytest.mark.asyncio
async def test_get_port_not_found(client):
    resp = await client.get(f"/ports/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_port_not_found(client):
    resp = await client.put(f"/ports/{uuid.uuid4()}", json={"name": "ghost"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_port_not_found(client):
    resp = await client.delete(f"/ports/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- Cascade and template delete ---


@pytest.mark.asyncio
async def test_delete_device_cascades_ports(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    # Delete device
    resp = await client.delete(f"/devices/{dev_id}")
    assert resp.status_code == 204
    # Port should be gone
    get_resp = await client.get(f"/ports/{port_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_port_template_blocked_by_ports(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    # Try to delete port template
    resp = await client.delete(f"/templates/{pt_id}")
    assert resp.status_code == 409
    assert "ports still reference" in resp.json()["detail"]


# --- Nonexistent device ports ---


@pytest.mark.asyncio
async def test_list_ports_nonexistent_device_returns_empty(client):
    """GET /devices/{random-uuid}/ports returns 200 with empty list."""
    resp = await client.get(f"/devices/{uuid.uuid4()}/ports")
    assert resp.status_code == 200
    assert resp.json() == []


# --- Bulk port creation ---


def _bulk_payload(port_template_id: str, **overrides) -> dict:
    base = {
        "name_prefix": "GigE0/1/",
        "starting_index": 1,
        "instances": 5,
        "template_id": port_template_id,
        "field_data": {"speed": "1G"},
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_bulk_create_ports(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data) == 5
    names = [p["name"] for p in data]
    assert names == ["GigE0/1/1", "GigE0/1/2", "GigE0/1/3", "GigE0/1/4", "GigE0/1/5"]
    for p in data:
        assert p["template_id"] == pt_id
        assert p["field_data"]["speed"] == "1G"


@pytest.mark.asyncio
async def test_bulk_create_ports_single_instance(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, instances=1),
    )
    assert resp.status_code == 201
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "GigE0/1/1"


@pytest.mark.asyncio
async def test_bulk_create_ports_starting_index_zero(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, starting_index=0, instances=3),
    )
    assert resp.status_code == 201
    names = [p["name"] for p in resp.json()]
    assert names == ["GigE0/1/0", "GigE0/1/1", "GigE0/1/2"]


@pytest.mark.asyncio
async def test_bulk_create_ports_invalid_instances_zero(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, instances=0),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_create_ports_negative_starting_index(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, starting_index=-1),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_create_ports_instances_exceeds_max(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, instances=201),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_create_ports_empty_prefix(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, name_prefix=""),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_create_ports_wrong_template_type(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(dt_id),
    )
    assert resp.status_code == 422
    assert "not a port template" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_bulk_create_ports_missing_required_field(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id, field_data={}),
    )
    assert resp.status_code == 422
    assert "speed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_bulk_create_ports_device_not_found(client):
    pt_id = await _create_port_template(client)
    resp = await client.post(
        f"/devices/{uuid.uuid4()}/ports/bulk",
        json=_bulk_payload(pt_id),
    )
    assert resp.status_code == 404
    assert "Device not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_port_invalid_field_data(client):
    """Update port with bad dropdown value returns 422."""
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    create_resp = await client.post(f"/devices/{dev_id}/ports", json=_port_payload(pt_id))
    port_id = create_resp.json()["id"]
    resp = await client.put(
        f"/ports/{port_id}",
        json={"field_data": {"speed": "100G"}},
    )
    assert resp.status_code == 422
    assert "speed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_user_cannot_bulk_create_ports(client):
    dt_id = await _create_device_template(client)
    dev_id = await _create_device(client, dt_id)
    pt_id = await _create_port_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.post(
        f"/devices/{dev_id}/ports/bulk",
        json=_bulk_payload(pt_id),
    )
    assert resp.status_code == 403
