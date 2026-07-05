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


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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


# --- Happy path ---


@pytest.mark.asyncio
async def test_create_dynamic_template(client):
    driver_id = await _create_driver(client, "Hypervisor", "Recipe A")
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": "Linux VM",
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["template_type"] == "dynamic"
    assert data["hypervisor_id"] == hid
    assert data["driver_id"] == driver_id
    # Dynamic templates are exempt from the vendor/model identity rule.
    assert data["vendor"] == "unknown"
    assert data["model"] == "unknown"


# --- Cross-field validation matrix ---


@pytest.mark.asyncio
async def test_dynamic_template_missing_driver_422(client):
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": "No Driver Dynamic",
            "template_type": "dynamic",
            "hypervisor_id": hid,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 422
    assert "driver" in str(resp.json()).lower()


@pytest.mark.asyncio
async def test_dynamic_template_missing_hypervisor_422(client):
    driver_id = await _create_driver(client, "Hypervisor", "Recipe B")
    resp = await client.post(
        "/templates",
        json={
            "name": "No HV Dynamic",
            "template_type": "dynamic",
            "driver_id": driver_id,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 422
    assert "hypervisor" in str(resp.json()).lower()


@pytest.mark.asyncio
async def test_hypervisor_id_on_device_template_422(client):
    driver_id = await _create_driver(client, "Management", "Mgmt A")
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": "Device With HV",
            "template_type": "device",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "vendor": "Cisco",
            "model": "X",
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 422
    assert "hypervisor_id is only valid on dynamic templates" in str(resp.json())


@pytest.mark.asyncio
async def test_dynamic_template_non_hypervisor_driver_422(client):
    # A management (non-Hypervisor) driver is rejected for a dynamic template at
    # the service layer.
    driver_id = await _create_driver(client, "Management", "Mgmt B")
    hid = await _create_hypervisor(client)
    resp = await client.post(
        "/templates",
        json={
            "name": "Dynamic Wrong Driver",
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Dynamic templates require a Hypervisor-type driver"


@pytest.mark.asyncio
async def test_device_template_hypervisor_driver_422(client):
    # The inverse: a Hypervisor driver is rejected for a device template.
    driver_id = await _create_driver(client, "Hypervisor", "Recipe C")
    resp = await client.post(
        "/templates",
        json={
            "name": "Device Wrong Driver",
            "template_type": "device",
            "driver_id": driver_id,
            "vendor": "Cisco",
            "model": "X",
            "sections": _SECTIONS,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Device templates cannot use a Hypervisor-type driver"
