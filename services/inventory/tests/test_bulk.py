"""Unit and functional tests for bulk import/export of devices and templates.

Covers: CSV and JSON round-trip, dry-run writing nothing, per-row error
handling (one bad row does not abort the batch), cross-instance reference
resolution by template name / driver name, and RBAC (admin-only).
"""

import io
import json

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
    from unittest.mock import patch

    with (
        patch("app.services.driver_service.upload_object", side_effect=mock_upload_object),
        patch("app.services.driver_service.delete_object", side_effect=mock_delete_object),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_reservation_guard():
    """Default the issue #391 delete guard to "no blocking reservations" so a
    device delete in this suite never reaches a real reservations service."""
    from unittest.mock import AsyncMock, patch

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


TEMPLATE_PAYLOAD = {
    "name": "Firewall",
    "icon": "data:image/png;base64,iVBOR",
    "sections": [
        {
            "name": "General",
            "fields": [
                {"key": "model", "label": "Model", "type": "string", "required": True},
                {"key": "rack", "label": "Rack", "type": "string"},
            ],
        },
    ],
}

_driver_counter = 0


async def _create_driver(client) -> str:
    global _driver_counter
    _driver_counter += 1
    resp = await client.post(
        "/drivers",
        data={"name": f"Driver {_driver_counter}", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(b"PK\x03\x04test"), "application/zip")},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_template(client, name="Firewall", vendor="Juniper", model="EX3300") -> dict:
    driver_id = await _create_driver(client)
    payload = {
        **TEMPLATE_PAYLOAD,
        "name": name,
        "driver_id": driver_id,
        "vendor": vendor,
        "model": model,
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_device(client, template_id, name="FW-01") -> dict:
    payload = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {"model": "EX3300", "rack": "A1"},
    }
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# Export ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_devices_json_carries_template_name_not_uuid(client):
    template = await _create_template(client)
    await _create_device(client, template["id"], name="FW-01")
    resp = await client.get("/devices/export", params={"format": "json"})
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["resource"] == "devices"
    assert len(doc["items"]) == 1
    item = doc["items"][0]
    assert item["name"] == "FW-01"
    assert item["template_name"] == "Firewall"
    assert "template_id" not in item


@pytest.mark.asyncio
async def test_export_devices_csv(client):
    template = await _create_template(client)
    await _create_device(client, template["id"], name="FW-01")
    resp = await client.get("/devices/export", params={"format": "csv"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert "name,template_name,topology_type" in body.splitlines()[0]
    assert "FW-01" in body
    assert "Firewall" in body


@pytest.mark.asyncio
async def test_export_templates_json_carries_driver_name(client):
    await _create_template(client, name="Firewall")
    resp = await client.get("/templates/export", params={"format": "json"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["name"] == "Firewall"
    assert items[0]["driver_name"] is not None
    assert "driver_id" not in items[0]


# Round-trip -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_then_device_json_roundtrip(client):
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="FW-01")

    tmpl_export = (await client.get("/templates/export", params={"format": "json"})).text
    dev_export = (await client.get("/devices/export", params={"format": "json"})).text

    # Simulate a fresh instance: drop and recreate the schema.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # Recreate the driver so the template import resolves driver_name.
    await _create_driver(client)
    # Rename driver to match export: export used "Driver N"; simplest is to
    # import templates with no driver dependency mismatch by re-exporting name.
    # Instead, verify device import resolves template_name after template import.

    tmpl_items = json.loads(tmpl_export)["items"]
    # Point the template at the freshly created driver by name.
    drivers = (await client.get("/drivers")).json()["items"]
    driver_name = drivers[0]["name"] if drivers else None
    if driver_name:
        for it in tmpl_items:
            it["driver_name"] = driver_name
    resp = await client.post(
        "/templates/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(tmpl_items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1
    assert report["rejected"] == 0

    resp = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(dev_export.encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1
    assert report["rejected"] == 0

    devices = await client.get("/devices")
    names = [d["name"] for d in devices.json()["items"]]
    assert "FW-01" in names


@pytest.mark.asyncio
async def test_template_reimport_omitting_vendor_model_preserves_them(client):
    """Issue #283: exporting a template, stripping vendor/model from the row, and
    re-importing takes the update path and preserves the stored (NOT NULL)
    vendor/model instead of nulling them and rejecting the row."""
    await _create_template(client, name="Firewall", vendor="Juniper", model="EX3300")
    items = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    assert items[0]["vendor"] == "Juniper"
    row = dict(items[0])
    row.pop("vendor", None)
    row.pop("model", None)
    resp = await client.post(
        "/templates/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps([row]).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["updated"] == 1
    assert report["created"] == 0
    assert report["rejected"] == 0
    after = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    assert after[0]["vendor"] == "Juniper"
    assert after[0]["model"] == "EX3300"


@pytest.mark.asyncio
async def test_template_reexport_reimport_is_noop_update(client):
    """The export/import round-trip invariant: re-importing an unedited export is
    an update that changes nothing (issue #283). Every exported field is applied
    back to its own value, so a second export matches the first byte-for-byte."""
    await _create_template(client, name="Firewall", vendor="Juniper", model="EX3300")
    before = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    resp = await client.post(
        "/templates/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(before).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["updated"] == 1
    assert report["created"] == 0
    assert report["rejected"] == 0
    after = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    assert after == before


@pytest.mark.asyncio
async def test_device_csv_roundtrip(client):
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="FW-01")
    csv_body = (await client.get("/devices/export", params={"format": "csv"})).text

    # Delete the device, re-import from CSV.
    devs = (await client.get("/devices")).json()["items"]
    await client.delete(f"/devices/{devs[0]['id']}")

    resp = await client.post(
        "/devices/import",
        params={"format": "csv"},
        files={"file": ("d.csv", io.BytesIO(csv_body.encode()), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1
    assert report["rejected"] == 0
    devices = (await client.get("/devices")).json()["items"]
    assert any(d["name"] == "FW-01" and d["field_data"].get("rack") == "A1" for d in devices)


# Update path ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_existing_device_is_update(client):
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="FW-01")
    items = [
        {
            "name": "FW-01",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "status": "MAINTENANCE",
            "field_data": {"model": "EX3300", "rack": "B2"},
            "poll_interval_seconds": None,
        }
    ]
    resp = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    report = resp.json()
    assert report["updated"] == 1
    assert report["created"] == 0
    devices = (await client.get("/devices")).json()["items"]
    fw = next(d for d in devices if d["name"] == "FW-01")
    assert fw["status"] == "MAINTENANCE"
    assert fw["field_data"]["rack"] == "B2"


# Dry run --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(client):
    await _create_template(client, name="Firewall")
    items = [
        {
            "name": "DRY-01",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
            "poll_interval_seconds": None,
        }
    ]
    resp = await client.post(
        "/devices/import",
        params={"format": "json", "dry_run": "true"},
        files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    report = resp.json()
    assert report["dry_run"] is True
    assert report["created"] == 1
    assert report["rows"][0]["action"] == "create"
    # Nothing was written.
    devices = (await client.get("/devices")).json()["items"]
    assert all(d["name"] != "DRY-01" for d in devices)


# Per-row error handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_row_does_not_abort_batch(client):
    await _create_template(client, name="Firewall")
    items = [
        {
            "name": "GOOD-01",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
        {
            "name": "BAD-01",
            "template_name": "DoesNotExist",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {},
        },
        {
            "name": "GOOD-02",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "Y"},
        },
    ]
    resp = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    report = resp.json()
    assert report["created"] == 2
    assert report["rejected"] == 1
    rejected = [r for r in report["rows"] if r["action"] == "reject"][0]
    assert rejected["identity"] == "BAD-01"
    assert "template not found" in rejected["reason"]
    devices = (await client.get("/devices")).json()["items"]
    created_names = {d["name"] for d in devices}
    assert {"GOOD-01", "GOOD-02"} <= created_names
    assert "BAD-01" not in created_names


@pytest.mark.asyncio
async def test_missing_name_is_rejected(client):
    await _create_template(client, name="Firewall")
    items = [{"template_name": "Firewall", "topology_type": "PHYSICAL", "field_data": {}}]
    resp = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    report = resp.json()
    assert report["rejected"] == 1
    assert "name" in report["rows"][0]["reason"]


# RBAC -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_requires_admin(user_client):
    resp = await user_client.get("/devices/export", params={"format": "json"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_import_requires_admin(user_client):
    resp = await user_client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(b"[]"), "application/json")},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_template_import_resolves_driver_by_name(client):
    # Create a driver so the imported template can resolve it by name.
    await _create_driver(client)
    drivers = (await client.get("/drivers")).json()["items"]
    driver_name = drivers[0]["name"]
    items = [
        {
            "name": "ImportedTemplate",
            "template_type": "device",
            "driver_name": driver_name,
            "exclusive": True,
            "vendor": "Acme",
            "model": "M1",
            "sections": TEMPLATE_PAYLOAD["sections"],
        }
    ]
    resp = await client.post(
        "/templates/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    report = resp.json()
    assert report["created"] == 1, report
    templates = (await client.get("/templates")).json()["items"]
    assert any(t["name"] == "ImportedTemplate" for t in templates)


@pytest.mark.asyncio
async def test_resolve_by_name_internal(client):
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="FW-01")
    await _create_device(client, template["id"], name="FW-02")
    resp = await client.post(
        "/devices/resolve-by-name",
        json={"names": ["FW-01", "FW-02", "MISSING"]},
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    resolved = resp.json()["resolved"]
    assert set(resolved.keys()) == {"FW-01", "FW-02"}


@pytest.mark.asyncio
async def test_resolve_by_name_requires_internal_token(client):
    resp = await client.post(
        "/devices/resolve-by-name",
        json={"names": ["FW-01"]},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_template_import_rejects_unknown_driver(client):
    items = [
        {
            "name": "T2",
            "template_type": "device",
            "driver_name": "NoSuchDriver",
            "vendor": "Acme",
            "model": "M1",
            "sections": TEMPLATE_PAYLOAD["sections"],
        }
    ]
    resp = await client.post(
        "/templates/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    report = resp.json()
    assert report["rejected"] == 1
    assert "driver not found" in report["rows"][0]["reason"]
