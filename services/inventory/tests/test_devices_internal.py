import io
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_SECRET_GET = "app.services.hypervisor_service.httpx.AsyncClient.get"
_TOKEN = {"X-Internal-Token": "test-token"}


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed the "No Pool" group so materialized devices can be auto-assigned,
    # matching the admin create path.
    from app.models.device_group import DeviceGroup

    async with TestSessionLocal() as session:
        session.add(DeviceGroup(name="No Pool", description="Default group"))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_minio():
    with patch("app.services.driver_service.upload_object", side_effect=lambda *a, **k: None):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_SECTIONS = [
    {
        "name": "Instance",
        "fields": [
            {"key": "image", "label": "Image", "type": "string", "required": True},
            {"key": "cpu", "label": "CPU", "type": "number", "default": 2},
        ],
    }
]


async def _create_driver(client, connection_type: str, name: str) -> str:
    content = b"PK\x03\x04test"
    resp = await client.post(
        "/drivers",
        data={"name": name, "connection_type": connection_type},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_hypervisor(client) -> str:
    payload = {
        "name": f"HV-{uuid.uuid4()}",
        "endpoint": "https://pve.example:8006",
        "hypervisor_type": "proxmox",
        "secret_id": str(uuid.uuid4()),
    }
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(200))):
        resp = await client.post("/hypervisors", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_dynamic_template(client, name="VM Template") -> str:
    driver_id = await _create_driver(client, "Hypervisor", f"Recipe {uuid.uuid4()}")
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": name,
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_device_template(client, name="Phys Template") -> str:
    driver_id = await _create_driver(client, "Management", f"Mgmt {uuid.uuid4()}")
    resp = await client.post(
        "/templates",
        json={
            "name": name,
            "template_type": "device",
            "driver_id": driver_id,
            "vendor": "Cisco",
            "model": "X",
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- Internal create happy path ---


@pytest.mark.asyncio
async def test_internal_create_generates_name(client):
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = uuid.uuid4()
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={"template_id": tid, "reservation_id": str(rid), "field_data": {"image": "ubuntu"}},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "RESERVED"
    assert data["topology_type"] == "CLOUD"
    # Generated name: "<template-name>-<first 8 of reservation>-<n>".
    assert data["name"] == f"Linux VM-{str(rid)[:8]}-1"
    # Default applied for cpu.
    assert data["field_data"]["cpu"] == 2
    assert data["field_data"]["image"] == "ubuntu"


@pytest.mark.asyncio
async def test_internal_create_name_collision_disambiguates(client):
    tid = await _create_dynamic_template(client, "Linux VM")
    rid = uuid.uuid4()
    body = {"template_id": tid, "reservation_id": str(rid), "field_data": {"image": "ubuntu"}}
    first = await client.post("/devices/internal", headers=_TOKEN, json=body)
    second = await client.post("/devices/internal", headers=_TOKEN, json=body)
    assert first.json()["name"].endswith("-1")
    assert second.json()["name"].endswith("-2")


@pytest.mark.asyncio
async def test_internal_create_explicit_name(client):
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "name": "my-vm-01",
            "field_data": {"image": "ubuntu"},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "my-vm-01"


@pytest.mark.asyncio
async def test_internal_create_allows_unknown_field_data(client):
    # The recipe's create_instance result contributes non-template keys, which
    # the internal path must accept (unlike the admin/device path).
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "field_data": {
                "image": "ubuntu",
                "management_address": "10.0.0.5",
                "instance_ref": "vm-900",
            },
        },
    )
    assert resp.status_code == 201
    fd = resp.json()["field_data"]
    assert fd["management_address"] == "10.0.0.5"
    assert fd["instance_ref"] == "vm-900"


@pytest.mark.asyncio
async def test_internal_create_rejects_non_dynamic_template_422(client):
    tid = await _create_device_template(client)
    resp = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "field_data": {"image": "x"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Template is not a dynamic template"


@pytest.mark.asyncio
async def test_internal_create_bad_token_403(client):
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices/internal",
        headers={"X-Internal-Token": "wrong"},
        json={"template_id": tid, "reservation_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


# --- Internal delete ---


@pytest.mark.asyncio
async def test_internal_delete_204(client):
    tid = await _create_dynamic_template(client)
    created = await client.post(
        "/devices/internal",
        headers=_TOKEN,
        json={
            "template_id": tid,
            "reservation_id": str(uuid.uuid4()),
            "field_data": {"image": "x"},
        },
    )
    device_id = created.json()["id"]
    resp = await client.delete(f"/devices/{device_id}/internal", headers=_TOKEN)
    assert resp.status_code == 204
    # Confirm it is gone.
    assert (await client.get(f"/devices/{device_id}/internal", headers=_TOKEN)).status_code == 404


@pytest.mark.asyncio
async def test_internal_delete_absent_404(client):
    resp = await client.delete(f"/devices/{uuid.uuid4()}/internal", headers=_TOKEN)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_internal_delete_non_dynamic_409(client):
    # A physical device (device template) may not be deleted through this surface.
    tid = await _create_device_template(client)
    created = await client.post(
        "/devices",
        json={
            "name": "phys-1",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"image": "x"},
        },
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]
    resp = await client.delete(f"/devices/{device_id}/internal", headers=_TOKEN)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Device is not a dynamic instance"


@pytest.mark.asyncio
async def test_internal_delete_bad_token_403(client):
    resp = await client.delete(
        f"/devices/{uuid.uuid4()}/internal", headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


# --- Admin device-create still rejects dynamic templates ---


@pytest.mark.asyncio
async def test_admin_create_device_rejects_dynamic_template_422(client):
    tid = await _create_dynamic_template(client)
    resp = await client.post(
        "/devices",
        json={
            "name": "should-fail",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"image": "x"},
        },
    )
    assert resp.status_code == 422
    # Rejected exactly as a "port" template is: "Template is not a device template".
    assert resp.json()["detail"] == "Template is not a device template"
