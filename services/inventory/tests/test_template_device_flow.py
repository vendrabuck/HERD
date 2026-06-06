"""End-to-end functional tests for the template-to-device flow."""

import io
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

_mock_storage: dict[str, bytes] = {}


def mock_upload_object(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def mock_delete_object(key: str) -> None:
    _mock_storage.pop(key, None)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


_driver_counter = 0


async def _create_driver(client, name_suffix="") -> str:
    global _driver_counter
    _driver_counter += 1
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={
            "name": f"Flow Driver {_driver_counter}{name_suffix}",
            "connection_type": "Management",
        },
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert drv_resp.status_code == 201
    return drv_resp.json()["id"]


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


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


ALL_TYPES_TEMPLATE = {
    "name": "All Types Template",
    "icon": "data:image/png;base64,AAAA",
    "description": "Template with all field types",
    "vendor": "TestVendor",
    "model": "TestModel",
    "sections": [
        {
            "name": "Config",
            "fields": [
                {"key": "hostname", "label": "Hostname", "type": "string", "required": True},
                {"key": "port_count", "label": "Port Count", "type": "number", "required": True},
                {"key": "managed", "label": "Managed", "type": "boolean"},
                {
                    "key": "region",
                    "label": "Region",
                    "type": "dropdown",
                    "options": ["US East", "US West", "EU Central"],
                },
            ],
        },
        {
            "name": "Metadata",
            "fields": [
                {"key": "notes", "label": "Notes", "type": "string"},
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_full_template_to_device_lifecycle(client):
    """Create template, create device, read, update, filter, delete device, delete template."""
    # 1. Create driver + template
    driver_id = await _create_driver(client)
    t_resp = await client.post("/templates", json={**ALL_TYPES_TEMPLATE, "driver_id": driver_id})
    assert t_resp.status_code == 201
    tid = t_resp.json()["id"]

    # 2. Create device from template
    device_payload = {
        "name": "DEV-LIFECYCLE",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {
            "hostname": "core-sw-01",
            "port_count": 48,
            "managed": True,
            "region": "US East",
            "notes": "Primary switch",
        },
    }
    d_resp = await client.post("/devices", json=device_payload)
    assert d_resp.status_code == 201
    did = d_resp.json()["id"]

    # 3. GET device; verify template info populated
    get_resp = await client.get(f"/devices/{did}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["template_name"] == "All Types Template"
    assert data["template_icon"] == "data:image/png;base64,AAAA"
    assert data["field_data"]["hostname"] == "core-sw-01"
    assert data["field_data"]["port_count"] == 48
    assert data["field_data"]["managed"] is True

    # 4. Update device field_data
    upd_resp = await client.put(
        f"/devices/{did}",
        json={
            "field_data": {
                "hostname": "core-sw-01-updated",
                "port_count": 96,
                "managed": False,
                "region": "US West",
            }
        },
    )
    assert upd_resp.status_code == 200
    assert upd_resp.json()["field_data"]["hostname"] == "core-sw-01-updated"

    # 5. Filter by template_id
    filter_resp = await client.get(f"/devices?template_id={tid}")
    assert filter_resp.status_code == 200
    assert len(filter_resp.json()["items"]) == 1

    # 6. Delete device
    del_resp = await client.delete(f"/devices/{did}")
    assert del_resp.status_code == 204

    # 7. Delete template (should succeed now)
    tdel_resp = await client.delete(f"/templates/{tid}")
    assert tdel_resp.status_code == 204


@pytest.mark.asyncio
async def test_template_update_does_not_break_existing_devices(client):
    """Updating a template (adding a new field) should not break existing devices."""
    # 1. Create driver, template, and device
    driver_id = await _create_driver(client)
    t_resp = await client.post("/templates", json={**ALL_TYPES_TEMPLATE, "driver_id": driver_id})
    tid = t_resp.json()["id"]

    d_resp = await client.post(
        "/devices",
        json={
            "name": "DEV-COMPAT",
            "template_id": tid,
            "topology_type": "PHYSICAL",
            "field_data": {
                "hostname": "sw-01",
                "port_count": 24,
            },
        },
    )
    assert d_resp.status_code == 201
    did = d_resp.json()["id"]

    # 2. Update template: add a new field to the first section
    updated_sections = list(ALL_TYPES_TEMPLATE["sections"])
    updated_first_section = dict(updated_sections[0])
    updated_first_section["fields"] = list(updated_first_section["fields"]) + [
        {"key": "firmware", "label": "Firmware Version", "type": "string"},
    ]
    updated_sections[0] = updated_first_section

    upd_resp = await client.put(f"/templates/{tid}", json={"sections": updated_sections})
    assert upd_resp.status_code == 200
    assert len(upd_resp.json()["sections"][0]["fields"]) == 5

    # 3. GET existing device (should still work)
    get_resp = await client.get(f"/devices/{did}")
    assert get_resp.status_code == 200
    assert get_resp.json()["field_data"]["hostname"] == "sw-01"

    # 4. Update device with the new field included
    new_data = dict(get_resp.json()["field_data"])
    new_data["firmware"] = "v4.2.1"
    upd_d = await client.put(f"/devices/{did}", json={"field_data": new_data})
    assert upd_d.status_code == 200
    assert upd_d.json()["field_data"]["firmware"] == "v4.2.1"


@pytest.mark.asyncio
async def test_multiple_templates_multiple_devices(client):
    """Multiple templates with multiple devices; filter, delete-blocked, cleanup."""
    # 1. Create drivers and two templates
    fw_driver_id = await _create_driver(client, "-fw")
    sw_driver_id = await _create_driver(client, "-sw")
    fw_template = {
        "name": "Firewall",
        "driver_id": fw_driver_id,
        "vendor": "TestVendor",
        "model": "FW-Model",
        "sections": [
            {
                "name": "Config",
                "fields": [
                    {"key": "model", "label": "Model", "type": "string", "required": True},
                    {
                        "key": "vendor",
                        "label": "Vendor",
                        "type": "dropdown",
                        "options": ["Juniper", "Fortinet"],
                    },
                ],
            }
        ],
    }
    sw_template = {
        "name": "Switch",
        "driver_id": sw_driver_id,
        "vendor": "TestVendor",
        "model": "SW-Model",
        "sections": [
            {
                "name": "Config",
                "fields": [
                    {"key": "ports", "label": "Ports", "type": "number", "required": True},
                ],
            }
        ],
    }
    fw_resp = await client.post("/templates", json=fw_template)
    assert fw_resp.status_code == 201
    fw_tid = fw_resp.json()["id"]

    sw_resp = await client.post("/templates", json=sw_template)
    assert sw_resp.status_code == 201
    sw_tid = sw_resp.json()["id"]

    # 2. Create devices from each template
    fw_dev = await client.post(
        "/devices",
        json={
            "name": "FW-01",
            "template_id": fw_tid,
            "topology_type": "PHYSICAL",
            "field_data": {"model": "EX3300", "vendor": "Juniper"},
        },
    )
    assert fw_dev.status_code == 201
    fw_did = fw_dev.json()["id"]

    sw_dev = await client.post(
        "/devices",
        json={
            "name": "SW-01",
            "template_id": sw_tid,
            "topology_type": "PHYSICAL",
            "field_data": {"ports": 48},
        },
    )
    assert sw_dev.status_code == 201
    sw_did = sw_dev.json()["id"]

    cloud_fw = await client.post(
        "/devices",
        json={
            "name": "Cloud-FW-01",
            "template_id": fw_tid,
            "topology_type": "CLOUD",
            "field_data": {"model": "vSRX", "vendor": "Juniper"},
        },
    )
    assert cloud_fw.status_code == 201
    cloud_fw_did = cloud_fw.json()["id"]

    # 3. Filter by template_id
    fw_filter = await client.get(f"/devices?template_id={fw_tid}")
    assert len(fw_filter.json()["items"]) == 2

    sw_filter = await client.get(f"/devices?template_id={sw_tid}")
    assert len(sw_filter.json()["items"]) == 1

    # 4. Filter by topology_type across templates
    phys_filter = await client.get("/devices?topology_type=PHYSICAL")
    assert len(phys_filter.json()["items"]) == 2

    cloud_filter = await client.get("/devices?topology_type=CLOUD")
    assert len(cloud_filter.json()["items"]) == 1

    # 5. Delete template with devices fails (409)
    del_blocked = await client.delete(f"/templates/{fw_tid}")
    assert del_blocked.status_code == 409

    # 6. Delete all devices for firewall template
    await client.delete(f"/devices/{fw_did}")
    await client.delete(f"/devices/{cloud_fw_did}")

    # 7. Delete firewall template succeeds
    del_ok = await client.delete(f"/templates/{fw_tid}")
    assert del_ok.status_code == 204

    # Cleanup
    await client.delete(f"/devices/{sw_did}")
    await client.delete(f"/templates/{sw_tid}")


@pytest.mark.asyncio
async def test_template_with_all_field_types(client):
    """Create template with all field types, validate correct and invalid values."""
    driver_id = await _create_driver(client)
    t_resp = await client.post("/templates", json={**ALL_TYPES_TEMPLATE, "driver_id": driver_id})
    assert t_resp.status_code == 201
    tid = t_resp.json()["id"]

    # Valid device with all field types
    valid_payload = {
        "name": "DEV-ALL-TYPES",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {
            "hostname": "test-host",
            "port_count": 24,
            "managed": True,
            "region": "EU Central",
            "notes": "test notes",
        },
    }
    resp = await client.post("/devices", json=valid_payload)
    assert resp.status_code == 201
    data = resp.json()["field_data"]
    assert data["hostname"] == "test-host"
    assert data["port_count"] == 24
    assert data["managed"] is True
    assert data["region"] == "EU Central"
    assert data["notes"] == "test notes"

    # Invalid: string where number expected
    bad_number = {
        "name": "DEV-BAD-NUM",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {"hostname": "h", "port_count": "not-a-number"},
    }
    resp = await client.post("/devices", json=bad_number)
    assert resp.status_code == 422
    assert "port_count" in resp.json()["detail"]

    # Invalid: number where string expected
    bad_string = {
        "name": "DEV-BAD-STR",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {"hostname": 12345, "port_count": 24},
    }
    resp = await client.post("/devices", json=bad_string)
    assert resp.status_code == 422
    assert "hostname" in resp.json()["detail"]

    # Invalid: string where boolean expected
    bad_bool = {
        "name": "DEV-BAD-BOOL",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {"hostname": "h", "port_count": 24, "managed": "yes"},
    }
    resp = await client.post("/devices", json=bad_bool)
    assert resp.status_code == 422
    assert "managed" in resp.json()["detail"]

    # Invalid: dropdown value not in options
    bad_dropdown = {
        "name": "DEV-BAD-DD",
        "template_id": tid,
        "topology_type": "PHYSICAL",
        "field_data": {"hostname": "h", "port_count": 24, "region": "Antarctica"},
    }
    resp = await client.post("/devices", json=bad_dropdown)
    assert resp.status_code == 422
    assert "region" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_full_port_lifecycle(client):
    """Device template, device, port template, add ports, update, delete, cascade, cleanup."""
    # 1. Create driver, device template, and device
    driver_id = await _create_driver(client)
    dt_resp = await client.post(
        "/templates",
        json={
            "name": "Router",
            "template_type": "device",
            "vendor": "V",
            "model": "M",
            "driver_id": driver_id,
            "sections": [
                {
                    "name": "Config",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string", "required": True},
                    ],
                },
            ],
        },
    )
    assert dt_resp.status_code == 201
    dt_id = dt_resp.json()["id"]

    dev_resp = await client.post(
        "/devices",
        json={
            "name": "RTR-01",
            "template_id": dt_id,
            "topology_type": "PHYSICAL",
            "field_data": {"model": "ISR-4451"},
        },
    )
    assert dev_resp.status_code == 201
    dev_id = dev_resp.json()["id"]

    # 2. Create port template
    pt_resp = await client.post(
        "/templates",
        json={
            "name": "SFP Port",
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
                            "options": ["1G", "10G"],
                        },
                        {"key": "label", "label": "Label", "type": "string"},
                    ],
                },
            ],
        },
    )
    assert pt_resp.status_code == 201
    pt_id = pt_resp.json()["id"]

    # 3. Add ports to device
    p1_resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "SFP0/1",
            "template_id": pt_id,
            "field_data": {"speed": "10G", "label": "uplink"},
        },
    )
    assert p1_resp.status_code == 201
    p1_id = p1_resp.json()["id"]

    p2_resp = await client.post(
        f"/devices/{dev_id}/ports",
        json={
            "name": "SFP0/2",
            "template_id": pt_id,
            "field_data": {"speed": "1G"},
        },
    )
    assert p2_resp.status_code == 201
    p2_id = p2_resp.json()["id"]

    # 4. List ports
    list_resp = await client.get(f"/devices/{dev_id}/ports")
    assert len(list_resp.json()) == 2

    # 5. Update port
    upd_resp = await client.put(
        f"/ports/{p1_id}",
        json={
            "name": "SFP0/1-uplink",
            "field_data": {"speed": "10G", "label": "primary-uplink"},
        },
    )
    assert upd_resp.status_code == 200
    assert upd_resp.json()["name"] == "SFP0/1-uplink"

    # 6. Delete one port
    del_resp = await client.delete(f"/ports/{p2_id}")
    assert del_resp.status_code == 204
    list_resp2 = await client.get(f"/devices/{dev_id}/ports")
    assert len(list_resp2.json()) == 1

    # 7. Delete device (cascades remaining ports)
    dev_del = await client.delete(f"/devices/{dev_id}")
    assert dev_del.status_code == 204
    get_port = await client.get(f"/ports/{p1_id}")
    assert get_port.status_code == 404

    # 8. Delete templates (should succeed now)
    assert (await client.delete(f"/templates/{pt_id}")).status_code == 204
    assert (await client.delete(f"/templates/{dt_id}")).status_code == 204
