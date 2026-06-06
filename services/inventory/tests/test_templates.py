import io
import uuid
from unittest.mock import patch

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


_mock_storage: dict[str, bytes] = {}


def mock_upload_object(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def mock_delete_object(key: str) -> None:
    _mock_storage.pop(key, None)


TEMPLATE_PAYLOAD = {
    "name": "Firewall",
    "template_type": "port",
    "icon": "data:image/png;base64,iVBOR",
    "description": "Standard firewall template",
    "sections": [
        {
            "name": "General",
            "fields": [
                {"key": "model", "label": "Model", "type": "string", "required": True},
                {"key": "interfaces", "label": "Interfaces", "type": "number"},
                {"key": "ha_enabled", "label": "HA Enabled", "type": "boolean"},
                {
                    "key": "vendor",
                    "label": "Vendor",
                    "type": "dropdown",
                    "options": ["Juniper", "Fortinet", "Cisco"],
                },
            ],
        },
        {
            "name": "Location",
            "fields": [
                {"key": "rack", "label": "Rack", "type": "string"},
                {"key": "unit", "label": "Unit", "type": "number"},
            ],
        },
    ],
}


async def create_driver_and_device_template(client, name="Device Template"):
    """Create a driver + device template; returns the template response JSON."""
    content = b"PK\x03\x04test"
    resp = await client.post(
        "/drivers",
        data={"name": f"Driver for {name}", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    driver_id = resp.json()["id"]
    payload = {
        **TEMPLATE_PAYLOAD,
        "name": name,
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "TestVendor",
        "model": "TestModel-1",
    }
    tmpl_resp = await client.post("/templates", json=payload)
    return tmpl_resp


# --- CRUD happy paths ---


@pytest.mark.asyncio
async def test_create_template(client):
    resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Firewall"
    assert data["icon"] == "data:image/png;base64,iVBOR"
    assert len(data["sections"]) == 2
    assert data["sections"][0]["fields"][0]["key"] == "model"


@pytest.mark.asyncio
async def test_list_templates(client):
    await client.post("/templates", json=TEMPLATE_PAYLOAD)
    resp = await client.get("/templates")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_get_template(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    resp = await client.get(f"/templates/{template_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == template_id


@pytest.mark.asyncio
async def test_update_template(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    resp = await client.put(
        f"/templates/{template_id}",
        json={"name": "Firewall v2", "description": "Updated"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Firewall v2"
    assert resp.json()["description"] == "Updated"


@pytest.mark.asyncio
async def test_update_template_sections(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    new_sections = [
        {
            "name": "Basic",
            "fields": [
                {"key": "hostname", "label": "Hostname", "type": "string", "required": True},
            ],
        }
    ]
    resp = await client.put(f"/templates/{template_id}", json={"sections": new_sections})
    assert resp.status_code == 200
    assert len(resp.json()["sections"]) == 1
    assert resp.json()["sections"][0]["name"] == "Basic"


@pytest.mark.asyncio
async def test_delete_template(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    resp = await client.delete(f"/templates/{template_id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/templates/{template_id}")
    assert get_resp.status_code == 404


# --- Validation tests ---


@pytest.mark.asyncio
async def test_create_template_empty_sections(client):
    payload = {**TEMPLATE_PAYLOAD, "sections": []}
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dropdown_without_options(client):
    payload = {
        "name": "Bad Template",
        "sections": [
            {
                "name": "Section",
                "fields": [
                    {"key": "choice", "label": "Choice", "type": "dropdown"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_non_dropdown_with_options(client):
    payload = {
        "name": "Bad Template",
        "sections": [
            {
                "name": "Section",
                "fields": [
                    {
                        "key": "name",
                        "label": "Name",
                        "type": "string",
                        "options": ["a", "b"],
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


# --- Permission tests ---


@pytest.mark.asyncio
async def test_user_can_list_templates(user_client):
    resp = await user_client.get("/templates")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_can_get_template(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    # Switch to user auth
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.get(f"/templates/{template_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_cannot_create_template(user_client):
    resp = await user_client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_update_template(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.put(f"/templates/{template_id}", json={"name": "Hacked"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_delete_template(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.delete(f"/templates/{template_id}")
    assert resp.status_code == 403


# --- 404 tests ---


@pytest.mark.asyncio
async def test_get_template_not_found(client):
    resp = await client.get(f"/templates/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_template_not_found(client):
    resp = await client.put(f"/templates/{uuid.uuid4()}", json={"name": "Ghost"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_template_not_found(client):
    resp = await client.delete(f"/templates/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- Additional validation tests ---


@pytest.mark.asyncio
async def test_create_template_with_multiword_options(client):
    payload = {
        "name": "Network Appliance",
        "template_type": "port",
        "sections": [
            {
                "name": "General",
                "fields": [
                    {
                        "key": "vendor",
                        "label": "Vendor",
                        "type": "dropdown",
                        "options": ["Juniper Networks", "Cisco Systems", "Check Point"],
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    options = data["sections"][0]["fields"][0]["options"]
    assert "Juniper Networks" in options
    assert "Cisco Systems" in options


@pytest.mark.asyncio
async def test_create_template_dropdown_empty_option_string(client):
    payload = {
        "name": "Bad Options",
        "sections": [
            {
                "name": "Section",
                "fields": [
                    {
                        "key": "choice",
                        "label": "Choice",
                        "type": "dropdown",
                        "options": ["valid", ""],
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_template_dropdown_empty_options_list(client):
    payload = {
        "name": "Empty Options",
        "sections": [
            {
                "name": "Section",
                "fields": [
                    {
                        "key": "choice",
                        "label": "Choice",
                        "type": "dropdown",
                        "options": [],
                    },
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_template_add_section(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    original_sections = TEMPLATE_PAYLOAD["sections"]
    new_section = {
        "name": "Management",
        "fields": [
            {"key": "mgmt_ip", "label": "Management IP", "type": "string"},
        ],
    }
    updated_sections = list(original_sections) + [new_section]
    resp = await client.put(f"/templates/{template_id}", json={"sections": updated_sections})
    assert resp.status_code == 200
    assert len(resp.json()["sections"]) == 3
    assert resp.json()["sections"][2]["name"] == "Management"


@pytest.mark.asyncio
async def test_update_template_remove_field(client):
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    trimmed_sections = [
        {
            "name": "General",
            "fields": [
                {"key": "model", "label": "Model", "type": "string", "required": True},
            ],
        }
    ]
    resp = await client.put(f"/templates/{template_id}", json={"sections": trimmed_sections})
    assert resp.status_code == 200
    assert len(resp.json()["sections"]) == 1
    assert len(resp.json()["sections"][0]["fields"]) == 1


@pytest.mark.asyncio
async def test_create_template_duplicate_name(client):
    await client.post("/templates", json=TEMPLATE_PAYLOAD)
    resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


# --- Field key and section name validation ---


@pytest.mark.asyncio
async def test_create_template_invalid_field_key(client):
    payload = {
        "name": "Bad Key",
        "sections": [
            {
                "name": "Section",
                "fields": [
                    {"key": "invalid key!", "label": "Label", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_template_empty_section_name(client):
    payload = {
        "name": "Empty Section",
        "sections": [
            {
                "name": "",
                "fields": [
                    {"key": "field1", "label": "Field", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_template_duplicate_field_keys(client):
    payload = {
        "name": "Dupe Keys",
        "sections": [
            {
                "name": "Section",
                "fields": [
                    {"key": "name", "label": "Name", "type": "string"},
                    {"key": "name", "label": "Name 2", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


# --- Template type tests ---


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_template_default_type_device(mock_up, client):
    resp = await create_driver_and_device_template(client, "Default Device")
    assert resp.status_code == 201
    assert resp.json()["template_type"] == "device"


@pytest.mark.asyncio
async def test_create_template_with_type_port(client):
    payload = {
        "name": "GigE Port",
        "template_type": "port",
        "sections": [
            {
                "name": "Config",
                "fields": [
                    {"key": "speed", "label": "Speed", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201
    assert resp.json()["template_type"] == "port"


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_list_templates_filter_by_type(mock_up, client):
    await create_driver_and_device_template(client, "Device Tmpl")
    await client.post(
        "/templates",
        json={
            "name": "GigE Port",
            "template_type": "port",
            "sections": [
                {
                    "name": "Config",
                    "fields": [
                        {"key": "speed", "label": "Speed", "type": "string"},
                    ],
                }
            ],
        },
    )
    # All templates
    resp = await client.get("/templates")
    assert len(resp.json()["items"]) == 2
    # Only device
    resp = await client.get("/templates?template_type=device")
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["template_type"] == "device"
    # Only port
    resp = await client.get("/templates?template_type=port")
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["template_type"] == "port"


# --- Icon update test ---


@pytest.mark.asyncio
async def test_update_template_icon(client):
    """PUT with new icon value persists it; setting icon to null clears it."""
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    assert create_resp.json()["icon"] == "data:image/png;base64,iVBOR"
    # Update icon
    new_icon = "data:image/png;base64,NEWICON"
    resp = await client.put(f"/templates/{template_id}", json={"icon": new_icon})
    assert resp.status_code == 200
    assert resp.json()["icon"] == new_icon
    # Clear icon by setting to null
    resp2 = await client.put(f"/templates/{template_id}", json={"icon": None})
    assert resp2.status_code == 200
    assert resp2.json()["icon"] is None


# --- Exclusive flag tests ---


@pytest.mark.asyncio
async def test_list_templates_empty(client):
    """No templates returns 200 + []."""
    resp = await client.get("/templates")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_update_template_duplicate_name(client):
    """Rename to existing name returns 409."""
    await client.post("/templates", json=TEMPLATE_PAYLOAD)
    resp2 = await client.post(
        "/templates",
        json={**TEMPLATE_PAYLOAD, "name": "Second Template"},
    )
    assert resp2.status_code == 201
    template2_id = resp2.json()["id"]
    resp = await client.put(f"/templates/{template2_id}", json={"name": "Firewall"})
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_template_exclusive_default(client):
    """Create template without specifying exclusive; verify response has exclusive: true."""
    resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert resp.status_code == 201
    assert resp.json()["exclusive"] is True


@pytest.mark.asyncio
async def test_create_template_non_exclusive(client):
    """Create template with exclusive: false; verify response."""
    payload = {**TEMPLATE_PAYLOAD, "name": "Shared Switch", "exclusive": False}
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201
    assert resp.json()["exclusive"] is False


@pytest.mark.asyncio
async def test_update_template_exclusive(client):
    """Update template exclusive flag from true to false."""
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert create_resp.status_code == 201
    template_id = create_resp.json()["id"]
    assert create_resp.json()["exclusive"] is True
    resp = await client.put(f"/templates/{template_id}", json={"exclusive": False})
    assert resp.status_code == 200
    assert resp.json()["exclusive"] is False


# --- Device template requires driver ---


@pytest.mark.asyncio
async def test_device_template_without_driver_returns_422(client):
    """Creating a device template without driver_id returns 422."""
    payload = {
        "name": "No Driver Device Tmpl",
        "template_type": "device",
        "sections": [
            {
                "name": "Config",
                "fields": [
                    {"key": "model", "label": "Model", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422
    assert "driver" in str(resp.json()).lower()


# --- Internal template endpoint ---


@pytest.mark.asyncio
async def test_internal_get_template(client):
    """GET /templates/{id}/internal with valid token returns template."""
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    resp = await client.get(
        f"/templates/{template_id}/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == template_id


@pytest.mark.asyncio
async def test_internal_get_template_bad_token(client):
    """GET /templates/{id}/internal with bad token returns 403."""
    create_resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    template_id = create_resp.json()["id"]
    resp = await client.get(
        f"/templates/{template_id}/internal",
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_get_template_not_found(client):
    """GET /templates/{id}/internal with valid token but nonexistent template returns 404."""
    resp = await client.get(
        f"/templates/{uuid.uuid4()}/internal",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_health_endpoint(client):
    """GET /health returns 200 with service info."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "inventory"


# --- Template hardware identity (vendor/model/part_number) ---


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_device_template_requires_vendor_and_model(mock_up, client):
    """Device templates must include vendor and model; missing -> 422."""
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={"name": "Identity Driver A", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    driver_id = drv_resp.json()["id"]
    payload = {
        **TEMPLATE_PAYLOAD,
        "name": "DeviceWithoutIdentity",
        "template_type": "device",
        "driver_id": driver_id,
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_device_template_rejects_whitespace_vendor(mock_up, client):
    """Whitespace-only vendor is rejected."""
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={"name": "Identity Driver B", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    driver_id = drv_resp.json()["id"]
    payload = {
        **TEMPLATE_PAYLOAD,
        "name": "DeviceWhitespaceVendor",
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "   ",
        "model": "Model-X",
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_create_device_template_with_identity_returns_fields(mock_up, client):
    """Vendor, model, and optional part_number round-trip on TemplateResponse."""
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={"name": "Identity Driver C", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    driver_id = drv_resp.json()["id"]
    payload = {
        **TEMPLATE_PAYLOAD,
        "name": "DeviceWithIdentity",
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "Cisco",
        "model": "Catalyst 9300",
        "part_number": "C9300-48UXM-A",
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["vendor"] == "Cisco"
    assert data["model"] == "Catalyst 9300"
    assert data["part_number"] == "C9300-48UXM-A"


@pytest.mark.asyncio
async def test_create_port_template_without_identity_succeeds(client):
    """Port templates remain exempt from required identity (TEMPLATE_PAYLOAD is port type)."""
    resp = await client.post("/templates", json=TEMPLATE_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["vendor"] == "unknown"
    assert data["model"] == "unknown"
    assert data["part_number"] is None


@pytest.mark.asyncio
@patch("app.services.driver_service.upload_object", side_effect=mock_upload_object)
async def test_update_template_can_clear_part_number(mock_up, client):
    """PUT can null out part_number on an existing device template."""
    create_resp = await create_driver_and_device_template(client, name="ClearPN")
    template_id = create_resp.json()["id"]
    set_resp = await client.put(f"/templates/{template_id}", json={"part_number": "ABC-123"})
    assert set_resp.status_code == 200
    assert set_resp.json()["part_number"] == "ABC-123"
    clear_resp = await client.put(f"/templates/{template_id}", json={"part_number": None})
    assert clear_resp.status_code == 200
    assert clear_resp.json()["part_number"] is None
