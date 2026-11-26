"""Unit and functional tests for bulk import/export of devices and templates.

Covers: CSV and JSON round-trip, dry-run writing nothing, per-row error
handling (one bad row does not abort the batch), cross-instance reference
resolution by template name / driver name, and RBAC (admin-only).
"""

import csv
import functools
import io
import json
import sys

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.services.bulk_service import (
    IMPORT_JSON_UNREADABLE_DETAIL,
    JSON_CELL_UNREADABLE_REASON,
    _coerce_json_cell,
    parse_import,
)
from fastapi import HTTPException
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
        "app.routers.devices.assert_device_deletable",
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
async def test_export_devices_csv_neutralizes_formula_trigger_cells(client):
    """issue #910: a device name or template_name beginning with a formula
    trigger (=, +, -, @, tab, CR) must be neutralized in the CSV export, not
    passed through for a spreadsheet to evaluate as a formula. topology_type
    and status are fixed enumerations and stay unquoted."""
    template = await _create_template(client, name="=cmd|' /c calc'!A0")
    await _create_device(client, template["id"], name="=1+1")
    resp = await client.get("/devices/export", params={"format": "csv"})
    assert resp.status_code == 200
    import csv as csv_module

    rows = list(csv_module.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "'=1+1"
    assert row["template_name"] == "'=cmd|' /c calc'!A0"
    assert row["topology_type"] == "PHYSICAL"
    assert row["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_export_templates_csv_neutralizes_formula_trigger_cells(client):
    """issue #910: the free-text template columns (name, driver_name, icon,
    description, vendor, model, part_number) are neutralized; template_type
    and exclusive are a fixed enumeration and a bool, unquoted; sections is a
    JSON blob whose first character is always "[" and needs no quoting."""
    driver_id = await _create_driver(client)
    payload = {
        **TEMPLATE_PAYLOAD,
        "name": "=1+1",
        "driver_id": driver_id,
        "vendor": "-vendor",
        "model": "@model",
        "part_number": "+partnum",
        "description": "\tdescription",
        "icon": "data:image/png;base64,iVBOR",
    }
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201, resp.text
    resp = await client.get("/templates/export", params={"format": "csv"})
    assert resp.status_code == 200
    import csv as csv_module

    rows = list(csv_module.DictReader(io.StringIO(resp.text)))
    row = rows[0]
    assert row["name"] == "'=1+1"
    assert row["vendor"] == "'-vendor"
    assert row["model"] == "'@model"
    assert row["part_number"] == "'+partnum"
    assert row["description"] == "'\tdescription"
    # icon is a data: URI, no trigger character leads it, so untouched.
    assert row["icon"] == "data:image/png;base64,iVBOR"
    assert row["template_type"] == "device"
    assert row["sections"].startswith("[")


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
async def test_device_csv_roundtrip_with_formula_trigger_name(client):
    """issue #910 round trip: exporting a device named "=1+1" and importing
    that CSV back in must restore the exact original name, never a name
    still carrying the export's neutralizing quote."""
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="=1+1")
    csv_body = (await client.get("/devices/export", params={"format": "csv"})).text
    # Sanity: the export really did neutralize the name.
    assert "'=1+1" in csv_body

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
    assert any(d["name"] == "=1+1" for d in devices)
    assert not any(d["name"] == "'=1+1" for d in devices)


@pytest.mark.asyncio
async def test_template_csv_roundtrip_with_formula_trigger_name(client):
    """issue #910 round trip: exporting a template named "=1+1" and
    importing that CSV back in (matched by name, so this is an update in
    place) must keep the exact original name, never rename it to a
    quote-carrying variant."""
    await _create_template(client, name="=1+1")
    csv_body = (await client.get("/templates/export", params={"format": "csv"})).text
    # Sanity: the export really did neutralize the name.
    assert "'=1+1" in csv_body

    resp = await client.post(
        "/templates/import",
        params={"format": "csv"},
        files={"file": ("t.csv", io.BytesIO(csv_body.encode()), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["updated"] == 1
    assert report["created"] == 0
    assert report["rejected"] == 0
    templates = (await client.get("/templates")).json()["items"]
    assert any(t["name"] == "=1+1" for t in templates)
    assert not any(t["name"] == "'=1+1" for t in templates)


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


# Encoding (issue #1022) -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/devices/import", "/templates/import"])
async def test_non_utf8_import_file_is_422_naming_the_encoding(client, path):
    """issue #1022: a Latin-1 CSV raised UnicodeDecodeError, a 500. It is a
    client input error: 422 naming the expected encoding, and nothing written."""
    await _create_template(client, name="Firewall")
    body = "name,template_name\ncaf\xe9,Firewall\n".encode("latin-1")
    resp = await client.post(
        path,
        params={"format": "csv"},
        files={"file": ("d.csv", io.BytesIO(body), "text/csv")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Import file must be UTF-8 encoded; re-save it as UTF-8 and retry"
    )
    devices = (await client.get("/devices")).json()["items"]
    assert devices == []


# Partial-column device update (issue #1016) ---------------------------------


async def _create_device_with_poll(client, template_id, name="FW-01") -> dict:
    payload = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {"model": "EX3300", "rack": "s3cret-rack"},
        "poll_interval_seconds": 60,
    }
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _import(client, body: bytes, fmt: str) -> dict:
    resp = await client.post(
        "/devices/import",
        params={"format": fmt},
        files={"file": (f"d.{fmt}", io.BytesIO(body), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _device_by_name(client, name: str) -> dict:
    devices = (await client.get("/devices")).json()["items"]
    return next(d for d in devices if d["name"] == name)


@pytest.mark.asyncio
async def test_device_json_reimport_omitting_columns_keeps_stored_values(client):
    """issue #1016: a JSON row without field_data or poll_interval_seconds
    used to replace field_data with {} and clear the poll interval."""
    template = await _create_template(client, name="Firewall")
    await _create_device_with_poll(client, template["id"])
    items = [{"name": "FW-01", "template_name": "Firewall", "status": "MAINTENANCE"}]
    report = await _import(client, json.dumps(items).encode(), "json")
    assert report["updated"] == 1, report
    assert report["rejected"] == 0
    fw = await _device_by_name(client, "FW-01")
    assert fw["status"] == "MAINTENANCE"
    assert fw["topology_type"] == "PHYSICAL"
    assert fw["field_data"] == {"model": "EX3300", "rack": "s3cret-rack"}
    assert fw["poll_interval_seconds"] == 60


@pytest.mark.asyncio
async def test_device_csv_export_drop_columns_reimport_keeps_omitted_values(client):
    """Round trip: export CSV, delete the field_data and poll columns, edit
    status, import. The omitted values survive and the edited one lands."""
    template = await _create_template(client, name="Firewall")
    await _create_device_with_poll(client, template["id"])
    csv_body = (await client.get("/devices/export", params={"format": "csv"})).text

    import csv as _csv

    rows = list(_csv.DictReader(io.StringIO(csv_body)))
    keep = ["name", "template_name", "topology_type", "status"]
    out = io.StringIO()
    writer = _csv.DictWriter(out, fieldnames=keep, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        row["status"] = "OFFLINE"
        writer.writerow(row)

    report = await _import(client, out.getvalue().encode(), "csv")
    assert report["updated"] == 1, report
    fw = await _device_by_name(client, "FW-01")
    assert fw["status"] == "OFFLINE"
    assert fw["field_data"] == {"model": "EX3300", "rack": "s3cret-rack"}
    assert fw["poll_interval_seconds"] == 60


@pytest.mark.asyncio
async def test_device_csv_name_and_template_only_is_a_no_op_update(client):
    """The issue's CSV repro: `name,template_name` for an existing device used
    to be rejected as "Device with name ... already exists" (a NOT NULL failure
    on the omitted status and topology_type). It is now an update that changes
    nothing."""
    template = await _create_template(client, name="Firewall")
    before = await _create_device_with_poll(client, template["id"])
    report = await _import(client, b"name,template_name\nFW-01,Firewall\n", "csv")
    assert report["updated"] == 1, report
    assert report["rejected"] == 0
    after = await _device_by_name(client, "FW-01")
    for key in ("status", "topology_type", "field_data", "poll_interval_seconds"):
        assert after[key] == before[key]


@pytest.mark.asyncio
async def test_device_import_create_without_topology_type_names_the_field(client):
    await _create_template(client, name="Firewall")
    items = [{"name": "NEW-01", "template_name": "Firewall", "field_data": {"model": "X"}}]
    report = await _import(client, json.dumps(items).encode(), "json")
    assert report["rejected"] == 1
    assert report["rows"][0]["reason"] == "missing required field: topology_type"
    assert all(d["name"] != "NEW-01" for d in (await client.get("/devices")).json()["items"])


@pytest.mark.asyncio
async def test_device_import_schema_error_reason_names_field_and_omits_input(client):
    """A schema error names the failing field and never echoes the row's
    input (str(ValidationError) carried the whole field_data)."""
    await _create_template(client, name="Firewall")
    items = [
        {
            "name": "NEW-02",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "status": "BROKEN",
            "field_data": {"model": "X", "rack": "s3cret-rack"},
        }
    ]
    report = await _import(client, json.dumps(items).encode(), "json")
    assert report["rejected"] == 1
    reason = report["rows"][0]["reason"]
    assert reason.startswith("status: Input should be")
    assert "s3cret-rack" not in reason
    assert "errors.pydantic.dev" not in reason


@pytest.mark.asyncio
async def test_put_device_explicit_null_status_is_422_naming_the_field(client):
    """issue #1016: `PUT /devices/{id}` with `{"status": null}` answered 409
    "Device with name ... already exists"."""
    template = await _create_template(client, name="Firewall")
    dev = await _create_device_with_poll(client, template["id"])
    resp = await client.put(f"/devices/{dev['id']}", json={"status": None})
    assert resp.status_code == 422
    assert [e["msg"] for e in resp.json()["detail"]] == [
        "Value error, status cannot be null; omit the field to leave it unchanged"
    ]
    assert (await _device_by_name(client, "FW-01"))["status"] == "AVAILABLE"


# Dry run is a full rehearsal (issue #1017) -----------------------------------


async def _post_import(client, resource: str, items: list, *, dry_run: bool) -> dict:
    resp = await client.post(
        f"/{resource}/import",
        params={"format": "json", "dry_run": "true" if dry_run else "false"},
        files={"file": ("x.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


_ONE_SECTION = [{"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}]


def _without_flag(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "dry_run"}


async def _create_named_driver(client, name: str, connection_type: str) -> str:
    resp = await client.post(
        "/drivers",
        data={"name": name, "connection_type": connection_type},
        files={"file": ("driver.zip", io.BytesIO(b"PK\x03\x04test"), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_dry_run_rejects_unknown_field_data_key_like_the_commit(client):
    """The issue's first repro: dry run reported `create` for a row the
    committing import rejects in `create_device`'s validate_field_data."""
    await _create_template(client, name="Firewall")
    items = [
        {
            "name": "BOGUS-01",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "field_data": {"model": "X", "bogus": 1},
        }
    ]
    dry = await _post_import(client, "devices", items, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["rows"] == [
        {"row": 0, "action": "reject", "identity": "BOGUS-01", "reason": "Unknown fields: bogus"}
    ]
    real = await _post_import(client, "devices", items, dry_run=False)
    assert real["dry_run"] is False
    assert _without_flag(dry) == _without_flag(real)


@pytest.mark.asyncio
async def test_dry_run_rejects_hypervisor_driver_on_device_template_like_the_commit(client):
    """The issue's second repro: the template-type versus driver
    connection-type rule lives in create_template, which a dry run skipped."""
    await _create_named_driver(client, "Recipe X", "Hypervisor")
    items = [
        {
            "name": "BadDevTpl",
            "template_type": "device",
            "driver_name": "Recipe X",
            "vendor": "V",
            "model": "M",
            "sections": _ONE_SECTION,
        }
    ]
    dry = await _post_import(client, "templates", items, dry_run=True)
    assert dry["rows"][0]["action"] == "reject"
    assert dry["rows"][0]["reason"] == "Device templates cannot use a Hypervisor-type driver"
    real = await _post_import(client, "templates", items, dry_run=False)
    assert _without_flag(dry) == _without_flag(real)
    templates = (await client.get("/templates")).json()["items"]
    assert all(t["name"] != "BadDevTpl" for t in templates)


@pytest.mark.asyncio
async def test_device_dry_run_report_matches_commit_row_for_row_and_writes_nothing(client):
    """A mixed file: a good create, a field-type error, a required-field
    error, a port template named in a device row, a duplicate name inside the
    file (the commit creates then updates it), and an update of an existing
    device. The dry-run report equals the committing report, and the dry run
    leaves every row, the No Pool membership included, as it was."""
    template = await _create_template(client, name="Firewall")
    existing = await _create_device(client, template["id"], name="FW-OLD")
    port_tpl = await client.post(
        "/templates",
        json={"name": "PortTpl", "template_type": "port", "sections": _ONE_SECTION},
    )
    assert port_tpl.status_code == 201, port_tpl.text
    items = [
        {"name": "OK-1", "template_name": "Firewall", "topology_type": "PHYSICAL",
         "field_data": {"model": "X"}},
        {"name": "TYPE-1", "template_name": "Firewall", "topology_type": "PHYSICAL",
         "field_data": {"model": 5}},
        {"name": "REQ-1", "template_name": "Firewall", "topology_type": "PHYSICAL",
         "field_data": {}},
        {"name": "PORT-1", "template_name": "PortTpl", "topology_type": "PHYSICAL",
         "field_data": {}},
        {"name": "OK-1", "template_name": "Firewall", "topology_type": "PHYSICAL",
         "status": "MAINTENANCE", "field_data": {"model": "Y"}},
        {"name": "FW-OLD", "template_name": "Firewall", "field_data": {"model": "Z", "bad": 1}},
        {"name": "FW-OLD", "template_name": "Firewall", "status": "MAINTENANCE"},
    ]  # fmt: skip
    before = sorted(
        (d["name"], d["status"], json.dumps(d["field_data"], sort_keys=True))
        for d in (await client.get("/devices")).json()["items"]
    )
    groups_before = (await client.get("/device-groups")).json()

    dry = await _post_import(client, "devices", items, dry_run=True)
    assert [r["action"] for r in dry["rows"]] == [
        "create", "reject", "reject", "reject", "update", "reject", "update",
    ]  # fmt: skip
    assert dry["rows"][1]["reason"] == "Field 'model' must be a string"
    assert dry["rows"][2]["reason"] == "Required field missing: model"
    assert dry["rows"][3]["reason"] == "Template is not a device template"
    assert dry["rows"][5]["reason"] == "Unknown fields: bad"

    after_dry = sorted(
        (d["name"], d["status"], json.dumps(d["field_data"], sort_keys=True))
        for d in (await client.get("/devices")).json()["items"]
    )
    assert after_dry == before
    assert (await client.get("/device-groups")).json() == groups_before

    real = await _post_import(client, "devices", items, dry_run=False)
    assert _without_flag(dry) == _without_flag(real)
    assert (await _device_by_name(client, "OK-1"))["status"] == "MAINTENANCE"
    assert (await _device_by_name(client, "FW-OLD"))["id"] == existing["id"]


@pytest.mark.asyncio
async def test_template_dry_run_duplicate_name_in_file_matches_commit(client):
    """Two rows with one new template name: the commit creates the first and
    updates it with the second, and the rehearsal must say the same."""
    items = [
        {"name": "Twice", "template_type": "port", "sections": _ONE_SECTION, "description": "one"},
        {"name": "Twice", "template_type": "port", "description": "two"},
    ]
    dry = await _post_import(client, "templates", items, dry_run=True)
    assert [r["action"] for r in dry["rows"]] == ["create", "update"]
    templates = (await client.get("/templates")).json()["items"]
    assert all(t["name"] != "Twice" for t in templates)
    real = await _post_import(client, "templates", items, dry_run=False)
    assert _without_flag(dry) == _without_flag(real)


# Dynamic templates round-trip through hypervisor_name (issue #1024) ---------

_SECRET_GET = "app.services.hypervisor_service.httpx.AsyncClient.get"


async def _create_hypervisor(client, name: str) -> str:
    from unittest.mock import AsyncMock, patch

    import httpx

    payload = {
        "name": name,
        "endpoint": "https://pve.example:8006",
        "hypervisor_type": "proxmox",
        "secret_id": "00000000-0000-0000-0000-0000000000aa",
    }
    with patch(_SECRET_GET, new=AsyncMock(return_value=httpx.Response(200))):
        resp = await client.post("/hypervisors", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_dynamic_template(client, name: str, hypervisor_name: str) -> dict:
    driver_id = await _create_named_driver(client, f"Recipe for {name}", "Hypervisor")
    hid = await _create_hypervisor(client, hypervisor_name)
    resp = await client.post(
        "/templates",
        json={
            "name": name,
            "template_type": "dynamic",
            "driver_id": driver_id,
            "hypervisor_id": hid,
            "sections": _ONE_SECTION,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_export_templates_carries_hypervisor_name(client):
    await _create_dynamic_template(client, "Dyn", "pve-east")
    await _create_template(client, name="Firewall")
    items = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    by_name = {i["name"]: i for i in items}
    assert by_name["Dyn"]["hypervisor_name"] == "pve-east"
    assert "hypervisor_id" not in by_name["Dyn"]
    assert by_name["Firewall"]["hypervisor_name"] is None


@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.asyncio
async def test_dynamic_template_round_trips_into_an_instance_without_it(client, fmt):
    """Export, delete the template (the target instance never had it), and
    re-import: the dynamic template is created bound to the same hypervisor.
    The hypervisor name starts with a CSV formula trigger, so the CSV leg also
    proves the column is neutralized on export and restored on import."""
    tpl = await _create_dynamic_template(client, "Dyn", "=pve-east")
    exported = (await client.get("/templates/export", params={"format": fmt})).content
    if fmt == "csv":
        assert b"'=pve-east" in exported
    assert (await client.delete(f"/templates/{tpl['id']}")).status_code == 204

    resp = await client.post(
        "/templates/import",
        params={"format": fmt},
        files={"file": (f"t.{fmt}", io.BytesIO(exported), "text/plain")},
    )
    report = resp.json()
    assert report["rows"] == [{"row": 0, "action": "create", "identity": "Dyn", "reason": None}]
    templates = (await client.get("/templates")).json()["items"]
    created = next(t for t in templates if t["name"] == "Dyn")
    assert created["template_type"] == "dynamic"
    assert created["hypervisor_id"] == tpl["hypervisor_id"]


@pytest.mark.asyncio
async def test_template_import_rejects_unknown_hypervisor_name(client):
    await _create_named_driver(client, "Recipe Q", "Hypervisor")
    items = [
        {
            "name": "Dyn",
            "template_type": "dynamic",
            "driver_name": "Recipe Q",
            "hypervisor_name": "pve-gone",
            "sections": _ONE_SECTION,
        }
    ]
    report = await _post_import(client, "templates", items, dry_run=False)
    assert report["rows"] == [
        {
            "row": 0,
            "action": "reject",
            "identity": "Dyn",
            "reason": "hypervisor not found by name: 'pve-gone'",
        }
    ]


@pytest.mark.asyncio
async def test_template_import_hypervisor_on_device_template_is_rejected(client):
    await _create_hypervisor(client, "pve-east")
    await _create_named_driver(client, "Mgmt Q", "Management")
    items = [
        {
            "name": "DevTpl",
            "template_type": "device",
            "driver_name": "Mgmt Q",
            "hypervisor_name": "pve-east",
            "vendor": "V",
            "model": "M",
            "sections": _ONE_SECTION,
        }
    ]
    report = await _post_import(client, "templates", items, dry_run=False)
    assert report["rejected"] == 1
    assert "hypervisor_id is only valid on dynamic templates" in report["rows"][0]["reason"]


@pytest.mark.asyncio
async def test_template_reimport_moves_dynamic_template_to_named_hypervisor(client):
    tpl = await _create_dynamic_template(client, "Dyn", "pve-east")
    west = await _create_hypervisor(client, "pve-west")
    items = [{"name": "Dyn", "hypervisor_name": "pve-west"}]
    report = await _post_import(client, "templates", items, dry_run=False)
    assert report["updated"] == 1, report
    assert (await client.get(f"/templates/{tpl['id']}")).json()["hypervisor_id"] == west


# Instance devices are left out of the device export (issue #1068) -----------


async def _create_instance_device(client) -> dict:
    """A dynamic-instance device as execution makes one: a dynamic template,
    a booking's reservation id, and a request_id."""
    import uuid

    tpl = await _create_dynamic_template(client, "Dyn", "pve-east")
    resp = await client.post(
        "/devices/internal",
        headers={"X-Internal-Token": "test-token"},
        json={
            "template_id": tpl["id"],
            "reservation_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "field_data": {"k": "v"},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _exported_device_names(body: bytes, fmt: str) -> list[str]:
    if fmt == "json":
        return [i["name"] for i in json.loads(body)["items"]]
    import csv

    return [row["name"] for row in csv.DictReader(io.StringIO(body.decode()))]


@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.asyncio
async def test_device_export_leaves_out_instance_devices(client, fmt):
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="FW-01")
    instance = await _create_instance_device(client)
    listed = [d["name"] for d in (await client.get("/devices")).json()["items"]]
    assert instance["name"] in listed

    resp = await client.get("/devices/export", params={"format": fmt})
    assert resp.status_code == 200
    assert _exported_device_names(resp.content, fmt) == ["FW-01"]


@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.asyncio
async def test_device_export_reimported_on_its_own_stack_leaves_instance_device_alone(client, fmt):
    """Re-importing the export on the same stack recreates the physical device
    and writes no row over the live instance device (before #1068 its row was
    re-imported as an update)."""
    template = await _create_template(client, name="Firewall")
    fw = await _create_device(client, template["id"], name="FW-01")
    await _create_instance_device(client)
    exported = (await client.get("/devices/export", params={"format": fmt})).content
    assert (await client.delete(f"/devices/{fw['id']}")).status_code == 204

    resp = await client.post(
        "/devices/import",
        params={"format": fmt},
        files={"file": (f"d.{fmt}", io.BytesIO(exported), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["rows"] == [{"row": 0, "action": "create", "identity": "FW-01", "reason": None}]
    devices = (await client.get("/devices")).json()["items"]
    assert any(d["name"] == "FW-01" and d["field_data"].get("rack") == "A1" for d in devices)


@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.asyncio
async def test_device_export_with_instance_device_imports_clean_on_a_fresh_stack(client, fmt):
    """Templates then devices into a fresh stack: every device row is created
    and none is rejected. Before #1068 the instance device's row was rejected
    with `Template is not a device template`, since the dynamic template it
    names cannot hold a device."""
    template = await _create_template(client, name="Firewall")
    await _create_device(client, template["id"], name="FW-01")
    await _create_instance_device(client)
    drivers = [
        (d["name"], d["connection_type"]) for d in (await client.get("/drivers")).json()["items"]
    ]
    tmpl_export = (await client.get("/templates/export", params={"format": fmt})).content
    dev_export = (await client.get("/devices/export", params={"format": fmt})).content

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    for name, connection_type in drivers:
        await _create_named_driver(client, name, connection_type)
    await _create_hypervisor(client, "pve-east")

    tmpl_report = (
        await client.post(
            "/templates/import",
            params={"format": fmt},
            files={"file": (f"t.{fmt}", io.BytesIO(tmpl_export), "text/plain")},
        )
    ).json()
    assert tmpl_report["created"] == 2 and tmpl_report["rejected"] == 0, tmpl_report

    resp = await client.post(
        "/devices/import",
        params={"format": fmt},
        files={"file": (f"d.{fmt}", io.BytesIO(dev_export), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == [
        {"row": 0, "action": "create", "identity": "FW-01", "reason": None}
    ]


# A lone surrogate in an import file (issue #1456 class) ----------------------------
#
# The file is a multipart upload, which the shared body check passes through, and its
# JSON is parsed here: `json.loads` turns an escape such as \ud800 into a lone
# surrogate. A device name holding one made the report unencodable (a 500); one in
# `field_data` was stored, and every later read of the device could not encode it.

_SUR = chr(0xD800)  # built, so no source escape is decoded early
_NOT_TEXT = "Import file holds a lone surrogate escape, which is not valid text; fix it and retry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "items"),
    [
        ("/devices/import", [{"name": "d" + _SUR, "template_name": "Firewall"}]),
        (
            "/devices/import",
            [
                {
                    "name": "d1",
                    "template_name": "Firewall",
                    "topology_type": "PHYSICAL",
                    "field_data": {"model": "x" + _SUR},
                }
            ],
        ),
        ("/devices/import", {"items": [], "k" + _SUR: 1}),
        ("/templates/import", [{"name": "t" + _SUR}]),
    ],
    ids=["device_name", "device_field_data", "key", "template_name"],
)
async def test_a_json_import_holding_a_lone_surrogate_is_a_fixed_422(client, path, items):
    await _create_template(client, name="Firewall")
    resp = await client.post(
        path,
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": _NOT_TEXT}
    assert (await client.get("/devices")).json()["items"] == []
    assert [t["name"] for t in (await client.get("/templates")).json()["items"]] == ["Firewall"]


@pytest.mark.asyncio
async def test_a_csv_json_cell_holding_a_lone_surrogate_rejects_that_row_only(client):
    await _create_template(client, name="Firewall")
    body = (
        b"name,template_name,topology_type,field_data\n"
        b'd1,Firewall,PHYSICAL,"{""model"": ""x\\ud800""}"\n'
        b'd2,Firewall,PHYSICAL,"{""model"": ""ok \\ud83d\\udd11""}"\n'
    )
    report = await _import(client, body, "csv")
    assert [(r["identity"], r["action"]) for r in report["rows"]] == [
        ("d1", "reject"),
        ("d2", "create"),
    ]
    assert report["rows"][0]["reason"] == (
        "a JSON cell holds a lone surrogate escape, which is not valid text"
    )
    assert (await _device_by_name(client, "d2"))["field_data"] == {"model": "ok \U0001f511"}


@pytest.mark.asyncio
async def test_a_bulk_upload_over_the_bound_is_refused_before_any_parse(client, monkeypatch):
    """Issue #1417: `await file.read()` held the whole upload in memory several
    times over before it was parsed on the event loop."""
    from unittest.mock import patch

    from app.routers import bulk as bulk_router

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", 16)
    # A payload that would be a valid import if it were parsed: the refusal must
    # come from the size bound, not from the content.
    body = json.dumps([{"name": "d1", "template_name": "Firewall"}]).encode()
    assert len(body) > 16

    # The router imports this name directly, so patching it on the service module
    # would never reach the router (issue #1417 review).
    with patch("app.routers.bulk.import_devices", side_effect=AssertionError("parsed")) as parse:
        resp = await client.post(
            "/devices/import",
            params={"format": "json"},
            files={"file": ("big.json", io.BytesIO(body), "application/json")},
        )

    assert resp.status_code == 413
    assert resp.json()["detail"] == [
        {
            "type": "body_too_large",
            "loc": ["body"],
            "msg": "Request body is larger than 16 bytes",
        }
    ]
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_an_upload_at_the_bound_is_accepted(client, monkeypatch):
    from app.routers import bulk as bulk_router

    await _create_template(client, name="Firewall")
    items = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    body = json.dumps(items).encode()
    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(body))

    resp = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(body), "application/json")},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_bound_is_exact_one_byte_over_is_refused(client, monkeypatch):
    """The bound is a byte count, not a chunk count: a file at the bound is
    accepted and one a single byte over it is refused."""
    from app.routers import bulk as bulk_router

    await _create_template(client, name="Firewall")
    items = (await client.get("/templates/export", params={"format": "json"})).json()["items"]
    body = json.dumps(items).encode()

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(body))
    at_bound = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("at.json", io.BytesIO(body), "application/json")},
    )
    assert at_bound.status_code == 200, at_bound.text

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(body) - 1)
    over = await client.post(
        "/devices/import",
        params={"format": "json"},
        files={"file": ("over.json", io.BytesIO(body), "application/json")},
    )
    assert over.status_code == 413
    assert over.json()["detail"] == [
        {
            "type": "body_too_large",
            "loc": ["body"],
            "msg": f"Request body is larger than {len(body) - 1} bytes",
        }
    ]


@pytest.mark.asyncio
async def test_the_templates_import_is_held_to_the_same_bound(client, monkeypatch):
    """INV-BULK-23 says "an import upload", which covers templates too.

    Mutation-checked: reverting `import_templates_endpoint` to
    `await file.read()` makes this fail.
    """
    from unittest.mock import patch

    from app.routers import bulk as bulk_router

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", 16)
    # A valid template body, so the only thing that can refuse it is the size.
    body = json.dumps([{"name": "Firewall"}]).encode()
    assert len(body) > 16

    with patch("app.routers.bulk.import_templates", side_effect=AssertionError("parsed")) as parse:
        resp = await client.post(
            "/templates/import",
            params={"format": "json"},
            files={"file": ("big.json", io.BytesIO(body), "application/json")},
        )

    assert resp.status_code == 413
    detail = resp.json()["detail"][0]
    assert detail["type"] == "body_too_large"
    assert "Request body is larger than 16 bytes" in detail["msg"]
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_the_read_asks_for_exactly_one_byte_past_the_bound(monkeypatch):
    """The bound is exact because the read asks for one byte past it.

    Mutation-checked: replacing `file.read(limit + 1)` with `file.read()` while
    keeping the length check makes this fail.
    """
    from app.routers import bulk as bulk_router

    asked: list[int] = []

    class _File:
        async def read(self, size: int = -1) -> bytes:
            asked.append(size)
            return b""

    await bulk_router._read_capped(_File(), 4096)

    assert asked == [4097]


# A JSON import the parser refuses for its shape, not its syntax (issue #1511) ------
#
# `json.loads` raises RecursionError for a document nested past CPython's C recursion
# budget and a plain ValueError (not a JSONDecodeError) for an integer literal longer
# than `sys.get_int_max_str_digits()`, 4,300 digits by default; both were 500s. The
# depth bound is the interpreter's, not HERD's: on 3.12 and 3.13 it is the C recursion
# limit, on 3.14 a C stack budget, and `sys.setrecursionlimit` moves neither. Measured
# from a script's top level, bare `json.loads` refused at about 10,000 levels on 3.12
# and 3.13 and at about 43,000 to 52,000 on the 3.14 builds tried; each figure moves a
# few levels, or a few dozen on 3.14, with the depth of the call stack it runs on. So
# no test pins a depth: `_parser_depth_bound` measures where `json.loads` first refuses
# on the running interpreter, and the boundary cases sit `_DEPTH_MARGIN` levels either
# side of it, which covers the difference between the measuring call stack and the
# route's. `_DEEP` is past every bound measured (3.12 to 3.14) and, as arrays, 200,000
# bytes.

_UNREADABLE = "Invalid JSON: the document is nested too deeply or holds a number too long to read"
_CELL_UNREADABLE = "a JSON cell is nested too deeply or holds a number too long to read"
_DEPTH_MARGIN = 500
_DEEP = 100_000
_DIGITS_AT_LIMIT = "1" * 4300
_DIGITS_OVER_LIMIT = "1" * 4301


def _nest(depth: int, inner: str = "") -> str:
    return "[" * depth + inner + "]" * depth


@functools.cache
def _parser_depth_bound() -> int:
    """The fewest nested arrays `json.loads` refuses with RecursionError here."""
    lo, hi = 1, _DEEP
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            json.loads(_nest(mid))
        except RecursionError:
            hi = mid
        else:
            lo = mid + 1
    return lo


def test_the_bounds_these_tests_rest_on():
    """The preconditions, stated so a changed interpreter fails here, not obscurely."""
    assert IMPORT_JSON_UNREADABLE_DETAIL == _UNREADABLE
    assert JSON_CELL_UNREADABLE_REASON == _CELL_UNREADABLE
    assert sys.get_int_max_str_digits() == 4300
    bound = _parser_depth_bound()
    # Past the bound on this interpreter, so _DEEP is a refusal, not a parse.
    assert bound + _DEPTH_MARGIN < _DEEP
    # A CSV cell this deep (two characters a level) fits the csv module's field limit,
    # so the cell reaches json.loads rather than stopping in the CSV reader.
    assert 2 * (bound + _DEPTH_MARGIN) < csv.field_size_limit()


# Whole documents the parser refuses; each answers the one fixed detail.
_UNREADABLE_DOCS = {
    "deep_arrays": lambda bound: _nest(_DEEP),
    "deep_just_over_the_bound": lambda bound: _nest(bound + _DEPTH_MARGIN),
    "deep_objects": lambda bound: '{"a": ' * _DEEP + "1" + "}" * _DEEP,
    "deep_beside_items": lambda bound: '{"items": [], "x": ' + _nest(_DEEP) + "}",
    "long_int": lambda bound: _DIGITS_OVER_LIMIT,
    "negative_long_int": lambda bound: "-" + _DIGITS_OVER_LIMIT,
    "long_int_nested_in_a_row": lambda bound: (
        '[{"name": "d1", "field_data": {"a": [0, {"b": ' + _DIGITS_OVER_LIMIT + "}]}}]"
    ),
    "long_int_then_deep": lambda bound: "[" + _DIGITS_OVER_LIMIT + ", " + _nest(_DEEP) + "]",
    "deep_then_long_int": lambda bound: "[" + _nest(_DEEP, _DIGITS_OVER_LIMIT) + ", 1]",
}


@pytest.mark.parametrize("kind", list(_UNREADABLE_DOCS))
def test_parse_import_refuses_an_unreadable_document_with_the_fixed_detail(kind):
    doc = _UNREADABLE_DOCS[kind](_parser_depth_bound())
    with pytest.raises(HTTPException) as exc:
        parse_import(doc.encode(), "json", [])
    assert exc.value.status_code == 422
    assert exc.value.detail == _UNREADABLE


def test_parse_import_reads_a_document_just_under_the_depth_bound():
    depth = _parser_depth_bound() - _DEPTH_MARGIN
    items = parse_import(_nest(depth).encode(), "json", [])
    # Never compare the nested value itself: the comparison recurses as deep.
    assert isinstance(items, list)
    assert len(items) == 1


@pytest.mark.parametrize("sign", ["", "-"])
def test_parse_import_reads_an_integer_at_the_digit_limit(sign):
    items = parse_import(f"[{sign}{_DIGITS_AT_LIMIT}]".encode(), "json", [])
    assert items == [int(sign + _DIGITS_AT_LIMIT)]


def test_parse_import_keeps_the_decoder_position_for_a_syntax_error():
    with pytest.raises(HTTPException) as exc:
        parse_import(b"[", "json", [])
    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid JSON: Expecting value: line 1 column 2 (char 1)"


@pytest.mark.parametrize(
    "cell",
    [
        lambda bound: _nest(_DEEP),
        lambda bound: _nest(bound + _DEPTH_MARGIN),
        lambda bound: '{"model": ' + _DIGITS_OVER_LIMIT + "}",
        lambda bound: "[-" + _DIGITS_OVER_LIMIT + "]",
    ],
    ids=["deep", "deep_just_over_the_bound", "long_int", "negative_long_int"],
)
def test_a_json_cell_the_parser_refuses_raises_the_fixed_reason(cell):
    with pytest.raises(ValueError) as exc:
        _coerce_json_cell(cell(_parser_depth_bound()), {})
    assert type(exc.value) is ValueError
    assert str(exc.value) == _CELL_UNREADABLE


def test_a_json_cell_just_under_the_bounds_still_parses():
    depth = _parser_depth_bound() - _DEPTH_MARGIN
    nested = _coerce_json_cell(_nest(depth), [])
    assert isinstance(nested, list)
    assert len(nested) == 1
    assert _coerce_json_cell("[-" + _DIGITS_AT_LIMIT + "]", []) == [-int(_DIGITS_AT_LIMIT)]


def test_a_json_cell_syntax_error_keeps_the_decoder_error():
    with pytest.raises(json.JSONDecodeError):
        _coerce_json_cell("{", {})


async def _bulk_import_row(client, path: str) -> dict:
    """One valid row for the importer at `path`, so a refusal provably wrote nothing."""
    if path == "/devices/import":
        return {
            "name": "d1",
            "template_name": "Firewall",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "EX3300"},
        }
    driver_name = (await client.get("/drivers")).json()["items"][0]["name"]
    return {
        "name": "T2",
        "template_type": "device",
        "driver_name": driver_name,
        "vendor": "Acme",
        "model": "M1",
        "sections": TEMPLATE_PAYLOAD["sections"],
    }


async def _bulk_state(client) -> tuple[list[str], list[str]]:
    devices = sorted(d["name"] for d in (await client.get("/devices")).json()["items"])
    templates = sorted(t["name"] for t in (await client.get("/templates")).json()["items"])
    return devices, templates


# Fragments the parser refuses, placed beside the items or inside the valid row.
_UNREADABLE_FRAGMENTS = {
    "deep": lambda bound: _nest(_DEEP),
    "deep_just_over_the_bound": lambda bound: _nest(bound + _DEPTH_MARGIN),
    "long_int": lambda bound: _DIGITS_OVER_LIMIT,
    "negative_long_int": lambda bound: '[0, {"b": -' + _DIGITS_OVER_LIMIT + "}]",
    "long_int_then_deep": lambda bound: "[" + _DIGITS_OVER_LIMIT + ", " + _nest(_DEEP) + "]",
    "deep_then_long_int": lambda bound: _nest(_DEEP, _DIGITS_OVER_LIMIT),
}


def _with_fragment(row: dict, fragment: str, placement: str) -> bytes:
    row_json = json.dumps(row)
    if placement == "beside_items":
        return ('{"items": [' + row_json + '], "x": ' + fragment + "}").encode()
    return ('{"items": [' + row_json[:-1] + ', "extra": ' + fragment + "}]}").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/devices/import", "/templates/import"])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("fragment", list(_UNREADABLE_FRAGMENTS))
@pytest.mark.parametrize("placement", ["beside_items", "inside_a_row"])
async def test_an_unreadable_json_import_is_a_fixed_422_and_writes_nothing(
    client, path, dry_run, fragment, placement
):
    """Issue #1511: each of these answered 500 through the route."""
    await _create_template(client, name="Firewall")
    before = await _bulk_state(client)
    row = await _bulk_import_row(client, path)
    body = _with_fragment(row, _UNREADABLE_FRAGMENTS[fragment](_parser_depth_bound()), placement)
    resp = await client.post(
        path,
        params={"format": "json", "dry_run": dry_run},
        files={"file": ("d.json", io.BytesIO(body), "application/json")},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": _UNREADABLE}
    assert await _bulk_state(client) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/devices/import", "/templates/import"])
@pytest.mark.parametrize(
    "fragment",
    [
        lambda bound: _nest(bound - _DEPTH_MARGIN),
        lambda bound: "[" + _DIGITS_AT_LIMIT + ", -" + _DIGITS_AT_LIMIT + "]",
    ],
    ids=["deep_just_under_the_bound", "ints_at_the_digit_limit"],
)
async def test_a_json_import_just_under_the_parser_bounds_imports(client, path, fragment):
    """The other side of each boundary: the same file shape parses and its row lands."""
    await _create_template(client, name="Firewall")
    row = await _bulk_import_row(client, path)
    body = _with_fragment(row, fragment(_parser_depth_bound()), "beside_items")
    resp = await client.post(
        path,
        params={"format": "json"},
        files={"file": ("d.json", io.BytesIO(body), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1, resp.text
    devices, templates = await _bulk_state(client)
    assert row["name"] in devices + templates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cell",
    [
        lambda bound: _nest(bound + _DEPTH_MARGIN),
        lambda bound: '{""model"": ' + _DIGITS_OVER_LIMIT + "}",
        lambda bound: '{""model"": [0, -' + _DIGITS_OVER_LIMIT + "]}",
    ],
    ids=["deep_just_over_the_bound", "long_int", "negative_long_int"],
)
async def test_a_csv_json_cell_the_parser_refuses_rejects_that_row_only(client, cell):
    """Before issue #1511 the row was rejected with Python's own exception text."""
    await _create_template(client, name="Firewall")
    body = (
        "name,template_name,topology_type,field_data\n"
        f'd1,Firewall,PHYSICAL,"{cell(_parser_depth_bound())}"\n'
        'd2,Firewall,PHYSICAL,"{""model"": ""EX3300""}"\n'
    ).encode()
    report = await _import(client, body, "csv")
    assert [(r["identity"], r["action"]) for r in report["rows"]] == [
        ("d1", "reject"),
        ("d2", "create"),
    ]
    assert report["rows"][0]["reason"] == _CELL_UNREADABLE
    assert (await _bulk_state(client))[0] == ["d2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/devices/import", "/templates/import"])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "text",
    [
        lambda bound: _nest(_DEEP),
        lambda bound: _nest(bound + _DEPTH_MARGIN),
        lambda bound: '{"model": ' + _DIGITS_OVER_LIMIT + "}",
        lambda bound: '[{"b": [0, -' + _DIGITS_OVER_LIMIT + "]}]",
    ],
    ids=["deep", "deep_just_over_the_bound", "long_int", "negative_long_int"],
)
async def test_a_json_string_cell_the_parser_refuses_rejects_that_row_only(
    client, path, dry_run, text
):
    """A JSON import may carry `field_data` or `sections` as a JSON string; it goes
    through the same cell parse as a CSV cell and gets the same fixed reason. The
    file itself parses, so only that row is rejected and the next is judged alone."""
    await _create_template(client, name="Firewall")
    before = await _bulk_state(client)
    good = await _bulk_import_row(client, path)
    key = "field_data" if path == "/devices/import" else "sections"
    bad = {**good, "name": "bad-row", key: text(_parser_depth_bound())}
    resp = await client.post(
        path,
        params={"format": "json", "dry_run": dry_run},
        files={
            "file": ("d.json", io.BytesIO(json.dumps([bad, good]).encode()), "application/json")
        },
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert [(r["identity"], r["action"]) for r in report["rows"]] == [
        ("bad-row", "reject"),
        (good["name"], "create"),
    ]
    assert report["rows"][0]["reason"] == _CELL_UNREADABLE
    devices, templates = await _bulk_state(client)
    if dry_run:
        assert (devices, templates) == before
    else:
        assert "bad-row" not in devices + templates
        assert good["name"] in devices + templates
