"""Unit and functional tests for bulk import/export of topologies.

Covers: JSON and CSV export with device ids rewritten to names, JSON/CSV import
round-trip, cross-instance device-name resolution (mocked inventory call),
dry-run writing nothing, per-row error handling, unresolved device rejection,
and the existing validator rejecting an unreachable edge on import.
"""

import io
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models.connection import Connection
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

# Two real device ids that exist in this instance's inventory (the cabling
# service only stores connections by device id). The resolver maps names to
# these.
DEV_A = str(uuid.uuid4())
DEV_B = str(uuid.uuid4())

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


def _override_admin():
    return ADMIN_PAYLOAD


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_connection():
    """Seed a direct A-B cable so an edge between A and B is reachable."""
    async with TestSessionLocal() as session:
        session.add(
            Connection(
                device_a_id=uuid.UUID(DEV_A),
                port_a="eth0",
                device_b_id=uuid.UUID(DEV_B),
                port_b="eth0",
                created_by="seed",
            )
        )
        await session.commit()


def _canvas_with_names():
    """A canvas referencing devices by name (export/import wire format)."""
    return {
        "nodes": [
            {"id": "n1", "data": {"device": {"name": "switch-a"}, "label": "switch-a"}},
            {"id": "n2", "data": {"device": {"name": "switch-b"}, "label": "switch-b"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}},
        ],
    }


def _resolver(mapping):
    return patch(
        "app.services.bulk_service.resolve_device_names",
        new=AsyncMock(return_value=mapping),
    )


# Export ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_json_rewrites_device_id_to_name(admin_client):
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": DEV_A, "name": "switch-a"}}},
        ],
        "edges": [],
    }
    create = await admin_client.post("/topologies", json={"name": "Lab"})
    tid = create.json()["id"]
    await admin_client.put(f"/topologies/{tid}", json={"canvas_data": canvas})

    resp = await admin_client.get("/topologies/export", params={"format": "json"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    node = items[0]["canvas"]["nodes"][0]
    assert node["data"]["device"]["name"] == "switch-a"
    assert "id" not in node["data"]["device"]


@pytest.mark.asyncio
async def test_export_csv_flattens_edges(admin_client):
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": DEV_A, "name": "switch-a"}}},
            {"id": "n2", "data": {"device": {"id": DEV_B, "name": "switch-b"}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
    }
    create = await admin_client.post("/topologies", json={"name": "Lab"})
    tid = create.json()["id"]
    await admin_client.put(f"/topologies/{tid}", json={"canvas_data": canvas})

    resp = await admin_client.get("/topologies/export", params={"format": "csv"})
    assert resp.status_code == 200
    body = resp.text
    assert "topology_name,source_device,source_port,target_device" in body.splitlines()[0]
    assert "switch-a" in body
    assert "switch-b" in body
    assert "L1" in body


# Import ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_json_creates_topology(admin_client):
    await _seed_connection()
    items = [{"name": "Imported Lab", "canvas": _canvas_with_names()}]
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1
    assert report["rejected"] == 0
    listing = (await admin_client.get("/topologies")).json()["items"]
    assert any(t["name"] == "Imported Lab" for t in listing)


@pytest.mark.asyncio
async def test_import_resolves_device_ids_in_canvas(admin_client):
    await _seed_connection()
    items = [{"name": "Resolved Lab", "canvas": _canvas_with_names()}]
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    listing = (await admin_client.get("/topologies")).json()["items"]
    tid = next(t["id"] for t in listing if t["name"] == "Resolved Lab")
    detail = (await admin_client.get(f"/topologies/{tid}")).json()
    node_ids = {n["data"]["device"].get("id") for n in detail["canvas_data"]["nodes"]}
    assert DEV_A in node_ids and DEV_B in node_ids


@pytest.mark.asyncio
async def test_import_csv_roundtrip(admin_client):
    await _seed_connection()
    csv_body = (
        "topology_name,source_device,source_port,target_device,target_port,layer\n"
        "CSV Lab,switch-a,eth0,switch-b,eth0,L1\n"
    )
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "csv"},
            files={"file": ("t.csv", io.BytesIO(csv_body.encode()), "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(admin_client):
    await _seed_connection()
    items = [{"name": "Dry Lab", "canvas": _canvas_with_names()}]
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json", "dry_run": "true"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    report = resp.json()
    assert report["dry_run"] is True
    assert report["created"] == 1
    listing = (await admin_client.get("/topologies")).json()["items"]
    assert all(t["name"] != "Dry Lab" for t in listing)


@pytest.mark.asyncio
async def test_unresolved_device_name_is_rejected(admin_client):
    items = [{"name": "Bad Lab", "canvas": _canvas_with_names()}]
    # Only one of two names resolves.
    with _resolver({"switch-a": DEV_A}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    report = resp.json()
    assert report["rejected"] == 1
    assert "unresolved device names" in report["rows"][0]["reason"]
    assert "switch-b" in report["rows"][0]["reason"]


@pytest.mark.asyncio
async def test_unreachable_edge_rejected_by_validator(admin_client):
    # No connection seeded, so the A-B edge has no physical path.
    items = [{"name": "Unreachable Lab", "canvas": _canvas_with_names()}]
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    report = resp.json()
    assert report["rejected"] == 1
    assert "validation failed" in report["rows"][0]["reason"]
    listing = (await admin_client.get("/topologies")).json()["items"]
    assert all(t["name"] != "Unreachable Lab" for t in listing)


@pytest.mark.asyncio
async def test_one_bad_topology_does_not_abort_batch(admin_client):
    await _seed_connection()
    good = {"name": "Good Lab", "canvas": _canvas_with_names()}
    bad = {"name": "Bad Lab", "canvas": _canvas_with_names()}  # uses same names
    items = [good, bad]
    # Resolve only switch-a so the second still fails on the same names? Both use
    # the same names; instead make the bad one structurally invalid by referencing
    # an extra unresolved device.
    bad["canvas"]["nodes"].append(
        {"id": "n3", "data": {"device": {"name": "ghost"}, "label": "ghost"}}
    )
    bad["canvas"]["edges"].append({"id": "e2", "source": "n1", "target": "n3", "data": {}})
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    report = resp.json()
    assert report["created"] == 1
    assert report["rejected"] == 1
    listing = (await admin_client.get("/topologies")).json()["items"]
    assert any(t["name"] == "Good Lab" for t in listing)
    assert all(t["name"] != "Bad Lab" for t in listing)


@pytest.mark.asyncio
async def test_missing_name_rejected(admin_client):
    items = [{"canvas": {"nodes": [], "edges": []}}]
    with _resolver({}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    report = resp.json()
    assert report["rejected"] == 1
    assert "name" in report["rows"][0]["reason"]


@pytest.mark.asyncio
async def test_export_then_import_full_roundtrip(admin_client):
    await _seed_connection()
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": DEV_A, "name": "switch-a"}}},
            {"id": "n2", "data": {"device": {"id": DEV_B, "name": "switch-b"}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
    }
    create = await admin_client.post("/topologies", json={"name": "Origin"})
    tid = create.json()["id"]
    await admin_client.put(f"/topologies/{tid}", json={"canvas_data": canvas})
    exported = (await admin_client.get("/topologies/export", params={"format": "json"})).text

    # Re-import into the same instance under the exported wire format.
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(exported.encode()), "application/json")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1
