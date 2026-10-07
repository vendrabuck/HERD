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


# --- issue #1018: the contract holds on update, both directions ---


async def _device_template(client, driver_id: str, name: str = "Dev Tpl") -> dict:
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
    return resp.json()


async def _dynamic_template(client, driver_id: str, hid: str, name: str = "Dyn Tpl") -> dict:
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
    return resp.json()


@pytest.mark.asyncio
async def test_update_device_template_clearing_driver_is_422(client):
    driver_id = await _create_driver(client, "Management", "Mgmt U1")
    tpl = await _device_template(client, driver_id)
    resp = await client.put(f"/templates/{tpl['id']}", json={"driver_id": None})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Device templates must have a driver"
    assert (await client.get(f"/templates/{tpl['id']}")).json()["driver_id"] == driver_id


@pytest.mark.asyncio
async def test_update_device_template_to_hypervisor_driver_is_422(client):
    driver_id = await _create_driver(client, "Management", "Mgmt U2")
    recipe = await _create_driver(client, "Hypervisor", "Recipe U2")
    tpl = await _device_template(client, driver_id)
    resp = await client.put(f"/templates/{tpl['id']}", json={"driver_id": recipe})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Device templates cannot use a Hypervisor-type driver"


@pytest.mark.asyncio
async def test_update_device_template_adding_hypervisor_is_422(client):
    driver_id = await _create_driver(client, "Management", "Mgmt U3")
    hid = await _create_hypervisor(client)
    tpl = await _device_template(client, driver_id)
    resp = await client.put(f"/templates/{tpl['id']}", json={"hypervisor_id": hid})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "hypervisor_id is only valid on dynamic templates"


@pytest.mark.asyncio
async def test_update_dynamic_template_clearing_hypervisor_or_driver_is_422(client):
    recipe = await _create_driver(client, "Hypervisor", "Recipe U4")
    hid = await _create_hypervisor(client)
    tpl = await _dynamic_template(client, recipe, hid)
    resp = await client.put(f"/templates/{tpl['id']}", json={"hypervisor_id": None})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Dynamic templates must have a hypervisor"
    resp = await client.put(f"/templates/{tpl['id']}", json={"driver_id": None})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Dynamic templates must have a driver"
    after = (await client.get(f"/templates/{tpl['id']}")).json()
    assert (after["driver_id"], after["hypervisor_id"]) == (recipe, hid)


@pytest.mark.asyncio
async def test_update_template_driver_swap_within_contract_still_succeeds(client):
    old = await _create_driver(client, "Management", "Mgmt U5a")
    new = await _create_driver(client, "Management", "Mgmt U5b")
    tpl = await _device_template(client, old)
    resp = await client.put(f"/templates/{tpl['id']}", json={"driver_id": new})
    assert resp.status_code == 200, resp.text
    assert resp.json()["driver_id"] == new
    # An update that touches neither driver nor hypervisor is not re-checked.
    resp = await client.put(f"/templates/{tpl['id']}", json={"description": "d"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_update_driver_used_by_device_template_to_hypervisor_is_409(client):
    driver_id = await _create_driver(client, "Management", "Mgmt U6")
    await _device_template(client, driver_id)
    resp = await client.put(f"/drivers/{driver_id}", json={"connection_type": "Hypervisor"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Cannot change connection_type: device templates use this driver, "
        "and device templates cannot use a Hypervisor-type driver"
    )
    assert (await client.get(f"/drivers/{driver_id}")).json()["connection_type"] == "Management"


@pytest.mark.asyncio
async def test_update_recipe_used_by_dynamic_template_to_non_hypervisor_is_409(client):
    recipe = await _create_driver(client, "Hypervisor", "Recipe U7")
    hid = await _create_hypervisor(client)
    await _dynamic_template(client, recipe, hid)
    resp = await client.put(f"/drivers/{recipe}", json={"connection_type": "Management"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Cannot change connection_type: dynamic templates use this driver, "
        "and dynamic templates require a Hypervisor-type driver"
    )
    assert (await client.get(f"/drivers/{recipe}")).json()["connection_type"] == "Hypervisor"


@pytest.mark.asyncio
async def test_update_driver_connection_type_within_contract_still_succeeds(client):
    # A device template's driver may move between non-Hypervisor types, and an
    # unchanged Hypervisor type on a used recipe is not a change.
    driver_id = await _create_driver(client, "Management", "Mgmt U8")
    await _device_template(client, driver_id)
    resp = await client.put(f"/drivers/{driver_id}", json={"connection_type": "Layer 2 Switch"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["connection_type"] == "Layer 2 Switch"
    recipe = await _create_driver(client, "Hypervisor", "Recipe U8")
    hid = await _create_hypervisor(client)
    await _dynamic_template(client, recipe, hid)
    resp = await client.put(f"/drivers/{recipe}", json={"connection_type": "Hypervisor"})
    assert resp.status_code == 200, resp.text
