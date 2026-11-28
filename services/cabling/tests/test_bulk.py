"""Unit and functional tests for bulk import/export of topologies.

Covers: JSON and CSV export with device ids rewritten to names, JSON/CSV import
round-trip, cross-instance device-name resolution (mocked inventory call),
dry-run writing nothing, per-row error handling, unresolved device rejection,
and the existing validator rejecting an unreachable edge on import.

None of these tests are about device visibility (issue #908; see
test_visibility_oracle.py for that). The autouse `_unfiltered_visibility`
fixture below pins `resolve_caller_visibility` to always return None (the
admin, no-filter outcome), which is exactly this suite's pre-#908 behavior and
keeps every existing assertion here about ownership, reservation locks, and
validation unaffected by the new gate. The ASGI test client below sends no
Authorization header, which a real non-admin request always would; the mock
stands in for that header's absence.
"""

import asyncio
import concurrent.futures
import csv
import functools
import io
import json
import sys
import threading
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models.connection import Connection
from app.models.topology import Topology, TopologyVersion
from app.services.bulk_service import (
    IMPORT_JSON_UNREADABLE_DETAIL,
    IMPORT_NOT_TEXT_DETAIL,
    parse_json_topologies,
)
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

USER_ID = str(uuid.uuid4())
USER_PAYLOAD = {"sub": USER_ID, "username": "user1", "role": "user"}

# A third user who owns topologies the acting user did not create (issue #464).
OTHER_ID = str(uuid.uuid4())

# The pinned per-row reject reason for a non-admin updating another user's
# topology via import (issue #464). Asserted verbatim: the "not_authorized"
# prefix is the machine-readable token consumers may key on.
NOT_OWNED_REASON = (
    "not_authorized: topology was created by another user; "
    "only its creator or an admin can update it via import"
)

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


@pytest.fixture(autouse=True)
def _unfiltered_visibility():
    """Every test in this file predates issue #908 and is not about
    visibility; pin the new per-request lookup to the no-filter outcome so
    this suite keeps testing what it always tested. See module docstring."""
    with patch(
        "app.services.bulk_service.resolve_caller_visibility",
        new=AsyncMock(return_value=None),
    ):
        yield


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


def _override_user():
    return USER_PAYLOAD


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _blocking(reservations):
    """Patch the reservation-lock lookup to return a fixed blocking list."""
    return patch(
        "app.services.bulk_service.find_blocking_reservations",
        new=AsyncMock(return_value=reservations),
    )


async def _count_versions() -> int:
    async with TestSessionLocal() as session:
        return len((await session.execute(select(TopologyVersion))).scalars().all())


async def _import_json(client, items, **params):
    return await client.post(
        "/topologies/import",
        params={"format": "json", **params},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )


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


@pytest.mark.asyncio
async def test_export_csv_neutralizes_formula_trigger_cells(admin_client):
    """issue #910: a topology name, or a device/port name from the stored
    canvas, beginning with a formula trigger (=, +, -, @, tab, CR) must be
    neutralized in the CSV export (a leading single quote), not passed
    through for a spreadsheet to evaluate as a formula. Device and port
    names come from the canvas, not inventory, so any authenticated user who
    can name a device on their own topology controls this cell."""
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": DEV_A, "name": "=formula-device"}}},
            {"id": "n2", "data": {"device": {"id": DEV_B, "name": "switch-b"}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {"layer": "L1", "sourcePort": "+1+1", "targetPort": "eth0"},
            }
        ],
    }
    create = await admin_client.post("/topologies", json={"name": "=1+1"})
    tid = create.json()["id"]
    await admin_client.put(f"/topologies/{tid}", json={"canvas_data": canvas})

    resp = await admin_client.get("/topologies/export", params={"format": "csv"})
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["topology_name"] == "'=1+1"
    assert row["source_device"] == "'=formula-device"
    assert row["source_port"] == "'+1+1"
    # target_device and target_port carry no trigger and are untouched.
    assert row["target_device"] == "switch-b"
    assert row["target_port"] == "eth0"


@pytest.mark.asyncio
async def test_export_then_import_csv_roundtrips_formula_name(admin_client):
    """issue #910 round trip: exporting a topology named "=1+1" and
    immediately importing that CSV back in must restore the exact original
    name, never a name still carrying the export's neutralizing quote."""
    await _seed_connection()
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": DEV_A, "name": "switch-a"}}},
            {"id": "n2", "data": {"device": {"id": DEV_B, "name": "switch-b"}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {"layer": "L1", "sourcePort": "eth0", "targetPort": "eth0"},
            }
        ],
    }
    create = await admin_client.post("/topologies", json={"name": "=1+1"})
    tid = create.json()["id"]
    await admin_client.put(f"/topologies/{tid}", json={"canvas_data": canvas})

    export_resp = await admin_client.get("/topologies/export", params={"format": "csv"})
    csv_body = export_resp.text
    # Sanity: the export really did neutralize the name; otherwise this test
    # would prove nothing about the round trip.
    assert "'=1+1" in csv_body

    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        import_resp = await admin_client.post(
            "/topologies/import",
            params={"format": "csv"},
            files={"file": ("t.csv", io.BytesIO(csv_body.encode()), "text/csv")},
        )
    assert import_resp.status_code == 200, import_resp.text
    report = import_resp.json()
    # The existing topology named "=1+1" is matched by name (bulk_service's
    # re-import-updates-in-place rule) and updated, never duplicated under a
    # still-quoted name.
    assert report["created"] + report["updated"] == 1
    assert report["rejected"] == 0

    listing = (await admin_client.get("/topologies")).json()["items"]
    assert any(t["name"] == "=1+1" for t in listing)
    assert not any(t["name"] == "'=1+1" for t in listing)


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

    # Re-import into the same instance under the exported wire format. The
    # topology already exists by name, so this updates in place, not duplicates.
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(exported.encode()), "application/json")},
        )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["updated"] == 1
    assert report["created"] == 0
    listing = (await admin_client.get("/topologies")).json()["items"]
    assert len([t for t in listing if t["name"] == "Origin"]) == 1


# Update-by-name (issue #336) ------------------------------------------------


def _isolated_nodes_canvas():
    """A valid canvas with the two devices as isolated nodes (no edges)."""
    return {
        "nodes": [
            {"id": "n1", "data": {"device": {"name": "switch-a"}, "label": "switch-a"}},
            {"id": "n2", "data": {"device": {"name": "switch-b"}, "label": "switch-b"}},
        ],
        "edges": [],
    }


@pytest.mark.asyncio
async def test_reimport_changed_canvas_updates_in_place(admin_client):
    await _seed_connection()
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        first = await _import_json(admin_client, [{"name": "RT", "canvas": _canvas_with_names()}])
        assert first.json()["created"] == 1
        # Re-import the same name with a different (still valid) canvas.
        second = await _import_json(
            admin_client, [{"name": "RT", "canvas": _isolated_nodes_canvas()}]
        )
    report = second.json()
    assert report["updated"] == 1
    assert report["created"] == 0

    listing = (await admin_client.get("/topologies")).json()["items"]
    matches = [t for t in listing if t["name"] == "RT"]
    assert len(matches) == 1, "update-by-name must not create a duplicate"
    detail = (await admin_client.get(f"/topologies/{matches[0]['id']}")).json()
    assert detail["canvas_data"]["edges"] == [], "canvas should be updated to the new import"
    # Create wrote version 1; the changed re-import appended version 2.
    assert await _count_versions() == 2


@pytest.mark.asyncio
async def test_reimport_identical_is_noop_update(admin_client):
    await _seed_connection()
    items = [{"name": "RT", "canvas": _canvas_with_names()}]
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(admin_client, items)
        second = await _import_json(admin_client, items)
    report = second.json()
    assert report["updated"] == 1
    assert report["created"] == 0
    listing = (await admin_client.get("/topologies")).json()["items"]
    assert len([t for t in listing if t["name"] == "RT"]) == 1
    # A byte-identical re-import is a no-op: no new version row is appended.
    assert await _count_versions() == 1


@pytest.mark.asyncio
async def test_dry_run_update_writes_nothing(admin_client):
    await _seed_connection()
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(admin_client, [{"name": "RT", "canvas": _canvas_with_names()}])
        resp = await _import_json(
            admin_client, [{"name": "RT", "canvas": _isolated_nodes_canvas()}], dry_run="true"
        )
    report = resp.json()
    assert report["dry_run"] is True
    assert report["updated"] == 1
    # No write: the stored canvas still has its original edge and only version 1.
    listing = (await admin_client.get("/topologies")).json()["items"]
    tid = next(t["id"] for t in listing if t["name"] == "RT")
    detail = (await admin_client.get(f"/topologies/{tid}")).json()
    assert detail["canvas_data"]["edges"], "dry-run update must not rewrite the canvas"
    assert await _count_versions() == 1


# Dry run is a full rehearsal (issue #1064) ----------------------------------


def _without_flag(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "dry_run"}


async def _topology_state() -> list[tuple]:
    """Every stored topology and version, for a writes-nothing comparison."""
    async with TestSessionLocal() as session:
        topologies = (await session.execute(select(Topology))).scalars().all()
        versions = (await session.execute(select(TopologyVersion))).scalars().all()
        return sorted(
            [
                ("t", str(t.id), t.name, json.dumps(t.canvas_data, sort_keys=True))
                for t in topologies
            ]
            + [
                ("v", str(v.topology_id), str(v.version_number), v.description or "")
                for v in versions
            ]
        )


@pytest.mark.asyncio
async def test_dry_run_duplicate_new_name_matches_commit_row_for_row(admin_client):
    """The issue's repro: a file naming one NEW topology twice. The commit
    creates it on the first row and updates it on the second, so the dry run
    must report `create, update` too, not `create, create`. The file also
    carries an update of an existing topology and an unresolvable row, and the
    dry run writes nothing at all."""
    await _seed_connection()
    ghost = _canvas_with_names()
    ghost["nodes"].append({"id": "n3", "data": {"device": {"name": "ghost"}, "label": "ghost"}})
    items = [
        {"name": "Twice", "canvas": _canvas_with_names()},
        {"name": "Twice", "canvas": _isolated_nodes_canvas()},
        {"name": "Existing", "canvas": _isolated_nodes_canvas()},
        {"name": "Ghost", "canvas": ghost},
    ]
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(admin_client, [{"name": "Existing", "canvas": _canvas_with_names()}])
        before = await _topology_state()
        dry = (await _import_json(admin_client, items, dry_run="true")).json()
        assert await _topology_state() == before
        real = (await _import_json(admin_client, items)).json()

    assert dry["dry_run"] is True
    assert real["dry_run"] is False
    assert [r["action"] for r in dry["rows"]] == ["create", "update", "update", "reject"]
    assert dry["rows"][3]["reason"] == "unresolved device names: ghost"
    assert _without_flag(dry) == _without_flag(real)
    # The commit really did create then update: one "Twice", two versions.
    async with TestSessionLocal() as session:
        twice = (
            (await session.execute(select(Topology).where(Topology.name == "Twice")))
            .scalars()
            .all()
        )
        assert len(twice) == 1
        assert twice[0].canvas_data["edges"] == []
        versions = (
            (
                await session.execute(
                    select(TopologyVersion).where(TopologyVersion.topology_id == twice[0].id)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(v.version_number for v in versions) == [1, 2]


@pytest.mark.asyncio
async def test_dry_run_csv_matches_commit_row_for_row(admin_client):
    """CSV groups rows by topology name, so a name cannot repeat as two
    records; the report for a mixed CSV file (a create, an update of an
    existing topology, an unresolvable device) still matches the commit row
    for row, and the dry run writes nothing."""
    await _seed_connection()
    csv_body = (
        "topology_name,source_device,source_port,target_device,target_port,layer\n"
        "Fresh CSV,switch-a,eth0,switch-b,eth0,L1\n"
        "Existing,switch-b,eth0,switch-a,eth0,L1\n"
        "Ghost CSV,switch-a,eth0,ghost,eth0,L1\n"
    )

    async def _post(dry_run: str) -> dict:
        resp = await admin_client.post(
            "/topologies/import",
            params={"format": "csv", "dry_run": dry_run},
            files={"file": ("t.csv", io.BytesIO(csv_body.encode()), "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(admin_client, [{"name": "Existing", "canvas": _canvas_with_names()}])
        before = await _topology_state()
        dry = await _post("true")
        assert await _topology_state() == before
        real = await _post("false")

    assert [r["action"] for r in dry["rows"]] == ["create", "update", "reject"]
    assert _without_flag(dry) == _without_flag(real)


@pytest.mark.asyncio
async def test_mixed_create_and_update_batch(admin_client):
    await _seed_connection()
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(admin_client, [{"name": "Existing", "canvas": _canvas_with_names()}])
        resp = await _import_json(
            admin_client,
            [
                {"name": "Existing", "canvas": _isolated_nodes_canvas()},
                {"name": "BrandNew", "canvas": _canvas_with_names()},
            ],
        )
    report = resp.json()
    assert report["total"] == 2
    assert report["created"] == 1
    assert report["updated"] == 1
    assert report["rejected"] == 0
    actions = {r["identity"]: r["action"] for r in report["rows"]}
    assert actions == {"Existing": "update", "BrandNew": "create"}


@pytest.mark.asyncio
async def test_update_blocked_by_other_users_active_reservation(user_client):
    await _seed_connection()
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(user_client, [{"name": "Locked", "canvas": _canvas_with_names()}])
        other = {"id": str(uuid.uuid4()), "status": "ACTIVE", "user_id": str(uuid.uuid4())}
        with _blocking([other]):
            resp = await _import_json(
                user_client, [{"name": "Locked", "canvas": _isolated_nodes_canvas()}]
            )
    report = resp.json()
    assert report["rejected"] == 1
    assert report["updated"] == 0
    row = report["rows"][0]
    assert row["identity"] == "Locked"
    assert row["reason"] == (
        "topology is in use by an active reservation owned by another user; "
        "bulk import cannot rewire it"
    )
    # The rewrite was refused: the stored canvas still has its original edge.
    listing = (await user_client.get("/topologies")).json()["items"]
    tid = next(t["id"] for t in listing if t["name"] == "Locked")
    detail = (await user_client.get(f"/topologies/{tid}")).json()
    assert detail["canvas_data"]["edges"], "locked topology wiring must be untouched"


@pytest.mark.asyncio
async def test_update_allowed_when_reservation_is_own(user_client):
    await _seed_connection()
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(user_client, [{"name": "Mine", "canvas": _canvas_with_names()}])
        own = {"id": str(uuid.uuid4()), "status": "ACTIVE", "user_id": USER_ID}
        with _blocking([own]):
            resp = await _import_json(
                user_client, [{"name": "Mine", "canvas": _isolated_nodes_canvas()}]
            )
    report = resp.json()
    assert report["updated"] == 1
    assert report["rejected"] == 0


@pytest.mark.asyncio
async def test_admin_bypasses_reservation_lock(admin_client):
    await _seed_connection()
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        await _import_json(admin_client, [{"name": "AdminLock", "canvas": _canvas_with_names()}])
        other = {"id": str(uuid.uuid4()), "status": "ACTIVE", "user_id": str(uuid.uuid4())}
        with _blocking([other]):
            resp = await _import_json(
                admin_client, [{"name": "AdminLock", "canvas": _isolated_nodes_canvas()}]
            )
    report = resp.json()
    assert report["updated"] == 1
    assert report["rejected"] == 0


# Creator-or-admin gate on the update path (issue #464) -----------------------


def _canvas_with_ids():
    """The stored form of _canvas_with_names(): device ids already resolved."""
    return {
        "nodes": [
            {
                "id": "n1",
                "data": {"device": {"id": DEV_A, "name": "switch-a"}, "label": "switch-a"},
            },
            {
                "id": "n2",
                "data": {"device": {"id": DEV_B, "name": "switch-b"}, "label": "switch-b"},
            },
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}},
        ],
    }


async def _seed_topology(name, created_by, canvas, created_at=None):
    """Seed a topology row directly so created_by can be an arbitrary user."""
    async with TestSessionLocal() as session:
        topology = Topology(
            name=name,
            created_by=uuid.UUID(created_by),
            owner_name="seeded",
            canvas_data=canvas,
        )
        if created_at is not None:
            topology.created_at = created_at
        session.add(topology)
        await session.commit()
        return str(topology.id)


async def _stored_canvas(topology_id):
    async with TestSessionLocal() as session:
        topology = await session.get(Topology, uuid.UUID(topology_id))
        return topology.canvas_data


@pytest.mark.asyncio
async def test_cross_owner_update_rejected_for_nonadmin(user_client):
    """A non-admin importing a name owned by another user is rejected per row
    with the pinned not_authorized reason; the victim canvas is untouched."""
    await _seed_connection()
    seeded = _canvas_with_ids()
    tid = await _seed_topology("Theirs", OTHER_ID, seeded)
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await _import_json(
            user_client, [{"name": "Theirs", "canvas": _isolated_nodes_canvas()}]
        )
    report = resp.json()
    assert report["rejected"] == 1
    assert report["updated"] == 0
    assert report["created"] == 0
    row = report["rows"][0]
    assert row["action"] == "reject"
    assert row["identity"] == "Theirs"
    assert row["reason"] == NOT_OWNED_REASON
    # The overwrite was refused: stored canvas is byte-identical to the seed
    # and no version row was appended.
    assert await _stored_canvas(tid) == seeded
    assert await _count_versions() == 0


@pytest.mark.asyncio
async def test_self_update_succeeds_for_nonadmin(user_client):
    """A non-admin updating their own topology by import still succeeds."""
    await _seed_connection()
    tid = await _seed_topology("Mine464", USER_ID, _canvas_with_ids())
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}), _blocking([]):
        resp = await _import_json(
            user_client, [{"name": "Mine464", "canvas": _isolated_nodes_canvas()}]
        )
    report = resp.json()
    assert report["updated"] == 1
    assert report["rejected"] == 0
    assert (await _stored_canvas(tid))["edges"] == []


@pytest.mark.asyncio
async def test_cross_owner_update_succeeds_for_admin(admin_client):
    """An admin may update another user's topology by import (matching PUT)."""
    await _seed_connection()
    tid = await _seed_topology("TheirsAdmin", OTHER_ID, _canvas_with_ids())
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await _import_json(
            admin_client, [{"name": "TheirsAdmin", "canvas": _isolated_nodes_canvas()}]
        )
    report = resp.json()
    assert report["updated"] == 1
    assert report["rejected"] == 0
    assert (await _stored_canvas(tid))["edges"] == []
    assert await _count_versions() == 1


@pytest.mark.asyncio
async def test_dry_run_reports_cross_owner_rejection_identically(user_client):
    """dry_run surfaces the same pinned rejection as a committing import."""
    await _seed_connection()
    seeded = _canvas_with_ids()
    tid = await _seed_topology("TheirsDry", OTHER_ID, seeded)
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await _import_json(
            user_client,
            [{"name": "TheirsDry", "canvas": _isolated_nodes_canvas()}],
            dry_run="true",
        )
    report = resp.json()
    assert report["dry_run"] is True
    assert report["rejected"] == 1
    assert report["updated"] == 0
    assert report["rows"][0]["reason"] == NOT_OWNED_REASON
    assert await _stored_canvas(tid) == seeded
    assert await _count_versions() == 0


@pytest.mark.asyncio
async def test_owned_match_preferred_over_older_foreign_topology(user_client):
    """When names collide, the actor's own topology is the update target even
    when another user's same-named topology is older; the foreign row is
    untouched and nothing is rejected (the accidental-clobber half of #464)."""
    await _seed_connection()
    foreign_canvas = _canvas_with_ids()
    foreign_tid = await _seed_topology(
        "Shared", OTHER_ID, foreign_canvas, created_at=datetime(2020, 1, 1)
    )
    own_tid = await _seed_topology(
        "Shared", USER_ID, _canvas_with_ids(), created_at=datetime(2024, 1, 1)
    )
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}), _blocking([]):
        resp = await _import_json(
            user_client, [{"name": "Shared", "canvas": _isolated_nodes_canvas()}]
        )
    report = resp.json()
    assert report["updated"] == 1
    assert report["rejected"] == 0
    assert (await _stored_canvas(own_tid))["edges"] == []
    assert await _stored_canvas(foreign_tid) == foreign_canvas


@pytest.mark.asyncio
async def test_admin_owned_match_preferred_on_name_collision(admin_client):
    """The owned-match preference applies to admins too: an admin importing
    their own export updates their own row, not another user's older one."""
    await _seed_connection()
    foreign_canvas = _canvas_with_ids()
    foreign_tid = await _seed_topology(
        "SharedAdmin", OTHER_ID, foreign_canvas, created_at=datetime(2020, 1, 1)
    )
    own_tid = await _seed_topology(
        "SharedAdmin", ADMIN_ID, _canvas_with_ids(), created_at=datetime(2024, 1, 1)
    )
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await _import_json(
            admin_client, [{"name": "SharedAdmin", "canvas": _isolated_nodes_canvas()}]
        )
    report = resp.json()
    assert report["updated"] == 1
    assert report["rejected"] == 0
    assert (await _stored_canvas(own_tid))["edges"] == []
    assert await _stored_canvas(foreign_tid) == foreign_canvas


@pytest.mark.asyncio
async def test_ownership_gate_precedes_reservation_lock(user_client):
    """A cross-owner match rejects on ownership, not the reservation lock,
    mirroring the interactive PUT's check order."""
    await _seed_connection()
    await _seed_topology("TheirsLocked", OTHER_ID, _canvas_with_ids())
    other = {"id": str(uuid.uuid4()), "status": "ACTIVE", "user_id": OTHER_ID}
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}), _blocking([other]):
        resp = await _import_json(
            user_client, [{"name": "TheirsLocked", "canvas": _isolated_nodes_canvas()}]
        )
    report = resp.json()
    assert report["rejected"] == 1
    assert report["rows"][0]["reason"] == NOT_OWNED_REASON


# CSV port columns (issue #1006) ----------------------------------------------


def _editor_canvas():
    """Two edges between the same devices, built the way the wiring dialog builds
    them: chosen ports on ``source_port_name``/``target_port_name`` and React Flow
    handle ids (never port names) on ``sourceHandle``/``targetHandle``."""
    return {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": DEV_A, "name": "switch-a"}}},
            {"id": "n2", "data": {"device": {"id": DEV_B, "name": "switch-b"}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "sourceHandle": "right",
                "targetHandle": "left",
                "data": {
                    "layer": "L1",
                    "source_port_name": "eth1",
                    "target_port_name": "eth1",
                },
            },
            {
                "id": "e2",
                "source": "n1",
                "target": "n2",
                "sourceHandle": "bottom",
                "targetHandle": "top",
                "data": {
                    "layer": "L1",
                    "source_port_name": "eth0",
                    "target_port_name": "eth0",
                },
            },
        ],
    }


async def _seed_second_connection():
    async with TestSessionLocal() as session:
        session.add(
            Connection(
                device_a_id=uuid.UUID(DEV_A),
                port_a="eth1",
                device_b_id=uuid.UUID(DEV_B),
                port_b="eth1",
                created_by="seed",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_export_csv_writes_editor_port_names_not_handles(admin_client):
    create = await admin_client.post("/topologies", json={"name": "Editor Lab"})
    await admin_client.put(
        f"/topologies/{create.json()['id']}", json={"canvas_data": _editor_canvas()}
    )
    resp = await admin_client.get("/topologies/export", params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert [(r["source_port"], r["target_port"]) for r in rows] == [
        ("eth1", "eth1"),
        ("eth0", "eth0"),
    ]
    assert not any(
        cell in ("right", "left", "top", "bottom")
        for r in rows
        for cell in (r["source_port"], r["target_port"])
    )


@pytest.mark.asyncio
async def test_export_csv_port_precedence_and_empty_cells(admin_client):
    """``*_port_name`` wins over the legacy ``sourcePort``; an edge with no chosen
    port exports an empty cell even when it carries a handle id."""
    canvas = {
        "nodes": _editor_canvas()["nodes"],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {
                    "source_port_name": "eth1",
                    "sourcePort": "legacy-src",
                    "targetPort": "legacy-tgt",
                },
            },
            {
                "id": "e2",
                "source": "n1",
                "target": "n2",
                "sourceHandle": "right",
                "targetHandle": "left",
                "data": {"layer": "L1"},
            },
        ],
    }
    create = await admin_client.post("/topologies", json={"name": "Precedence Lab"})
    await admin_client.put(f"/topologies/{create.json()['id']}", json={"canvas_data": canvas})
    resp = await admin_client.get("/topologies/export", params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert [(r["source_port"], r["target_port"]) for r in rows] == [
        ("eth1", "legacy-tgt"),
        ("", ""),
    ]


@pytest.mark.asyncio
async def test_csv_export_import_preserves_ports_through_fork_resolve(admin_client):
    """Export then import then fork resolve yields the same wires (issue #1006)."""
    from app.services.fork_save_service import resolve_canvas_wiring

    await _seed_connection()
    await _seed_second_connection()
    create = await admin_client.post("/topologies", json={"name": "Port Lab"})
    tid = create.json()["id"]
    await admin_client.put(f"/topologies/{tid}", json={"canvas_data": _editor_canvas()})

    async with TestSessionLocal() as session:
        before = await resolve_canvas_wiring(session, _editor_canvas())
    before_wires = sorted((s.port_a, s.port_b) for s in before.specs)
    assert before_wires == [("eth0", "eth0"), ("eth1", "eth1")]

    csv_body = (await admin_client.get("/topologies/export", params={"format": "csv"})).text
    # Rename the source so the import creates a fresh topology from the CSV alone.
    await admin_client.put(f"/topologies/{tid}", json={"name": "Port Lab source"})
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        imported = await admin_client.post(
            "/topologies/import",
            params={"format": "csv"},
            files={"file": ("t.csv", io.BytesIO(csv_body.encode()), "text/csv")},
        )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 1, imported.text

    listing = (await admin_client.get("/topologies")).json()["items"]
    new_id = next(t["id"] for t in listing if t["name"] == "Port Lab")
    canvas = (await admin_client.get(f"/topologies/{new_id}")).json()["canvas_data"]
    ports = [
        (e["data"].get("source_port_name"), e["data"].get("target_port_name"))
        for e in canvas["edges"]
    ]
    assert ports == [("eth1", "eth1"), ("eth0", "eth0")]
    assert all("sourcePort" not in e["data"] for e in canvas["edges"])

    async with TestSessionLocal() as session:
        after = await resolve_canvas_wiring(session, canvas)
    assert sorted((s.port_a, s.port_b) for s in after.specs) == before_wires


def test_parse_csv_empty_port_cell_leaves_side_unconstrained():
    from app.services.bulk_service import parse_csv_topologies

    body = (
        "topology_name,source_device,source_port,target_device,target_port,layer\n"
        "Lab,switch-a,'=eth0,switch-b,,L1\n"
    )
    [record] = parse_csv_topologies(body.encode())
    data = record["canvas"]["edges"][0]["data"]
    # The neutralizing quote comes back off (issue #910) and the empty cell is absent.
    assert data == {"layer": "L1", "source_port_name": "=eth0"}


# A lone surrogate in an imported JSON file ----------------------------------------
#
# The file is a multipart upload, which the shared body check passes through, and its
# JSON is parsed here: `json.loads` turns an escape such as \ud800 into a lone
# surrogate, which no response, database write, or later read can encode.

_SUR = chr(0xD800)  # built, so no source escape is decoded early


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [{"name": "lab" + _SUR, "canvas": {"nodes": [], "edges": []}}],
        [{"name": "lab", "canvas": {"nodes": [{"id": "n", "data": {"label": _SUR}}], "edges": []}}],
        {"items": [{"name": "lab", "canvas": {"nodes": [], "edges": []}}], "x" + _SUR: 1},
    ],
    ids=["name", "nested_canvas_value", "key"],
)
async def test_a_json_import_holding_a_lone_surrogate_is_a_fixed_422(admin_client, items):
    before = await _count_versions()
    resp = await admin_client.post(
        "/topologies/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": IMPORT_NOT_TEXT_DETAIL}
    assert IMPORT_NOT_TEXT_DETAIL == (
        "Import file holds a lone surrogate escape, which is not valid text; fix it and retry"
    )
    assert await _count_versions() == before


@pytest.mark.asyncio
async def test_a_json_import_with_a_surrogate_pair_escape_is_not_refused(admin_client):
    """A character outside the BMP written as two escapes is one code point once parsed."""
    items = [{"name": "lab \U0001f511", "canvas": {"nodes": [], "edges": []}}]
    resp = await admin_client.post(
        "/topologies/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "body"),
    [
        ("csv", "topology_name,source_device\ncaf\xe9,a\n".encode("latin-1")),
        ("json", '[{"name": "caf\xe9"}]'.encode("latin-1")),
        ("json", '[{"name": "x\ud800"}]'.encode("utf-8", "surrogatepass")),
    ],
    ids=["latin1_csv", "latin1_json", "raw_surrogate_bytes"],
)
async def test_a_non_utf8_import_file_is_422_naming_the_encoding(admin_client, fmt, body):
    """A file that is not UTF-8 raised UnicodeDecodeError, a 500, as it did on inventory
    before issue #1022. It is a client input error: a 422 naming the encoding."""
    before = await _count_versions()
    resp = await admin_client.post(
        "/topologies/import",
        params={"format": fmt},
        files={"file": (f"t.{fmt}", io.BytesIO(body), "text/plain")},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": "Import file must be UTF-8 encoded; re-save it as UTF-8 and retry"
    }
    assert await _count_versions() == before


@pytest.mark.asyncio
async def test_a_topology_upload_over_the_bound_is_a_413(admin_client, monkeypatch):
    """Issue #1417: `await file.read()` held the whole upload in memory before
    it was parsed, on the event loop every topology read depends on."""
    from unittest.mock import patch

    from app.routes import bulk as bulk_router

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", 16)
    body = json.dumps([{"name": "topology-one"}]).encode()
    assert len(body) > 16

    with patch("app.routes.bulk.import_topologies", side_effect=AssertionError("parsed")) as parse:
        resp = await admin_client.post(
            "/topologies/import",
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
async def test_the_bound_is_exact_one_byte_over_is_refused(admin_client, monkeypatch):
    """The cap is a byte count: at it is accepted, one byte over is refused.

    Mutation-checked: changing `len(raw) > limit` to `>=` makes this fail, and
    inventory already had the same test.
    """
    from app.routes import bulk as bulk_router

    body = b'[{"name": "topology-one"}]'

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(body))
    at_bound = await admin_client.post(
        "/topologies/import",
        params={"format": "json"},
        files={"file": ("at.json", io.BytesIO(body), "application/json")},
    )
    assert at_bound.status_code == 200, at_bound.text

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(body) - 1)
    over = await admin_client.post(
        "/topologies/import",
        params={"format": "json"},
        files={"file": ("over.json", io.BytesIO(body), "application/json")},
    )
    assert over.status_code == 413
    assert over.json()["detail"][0]["msg"] == (f"Request body is larger than {len(body) - 1} bytes")


@pytest.mark.asyncio
async def test_the_read_asks_for_exactly_one_byte_past_the_bound(monkeypatch):
    """Mutation-checked: `file.read()` with the length check kept fails this."""
    from app.routes import bulk as bulk_router

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
# than `sys.get_int_max_str_digits()`, 4,300 digits by default; both were 500s, which
# any signed-in user could reach with a 20 KB file. The depth bound is the
# interpreter's, not HERD's: on 3.12 and 3.13 it is the C recursion limit, on 3.14 a
# C stack budget, and `sys.setrecursionlimit` moves neither. Measured from a script's
# top level, bare `json.loads` refused at about 10,000 levels on 3.12 and 3.13 and at
# about 43,000 to 52,000 on the 3.14 builds tried; each figure moves a few levels, or a
# few dozen on 3.14, with the depth of the call stack it runs on. So no test pins a
# depth: `_parser_depth_bound` measures where `json.loads` first refuses on the running
# interpreter, and the boundary cases sit `_DEPTH_MARGIN` levels either side of it,
# which covers the difference between the measuring call stack and the route's.
# `_DEEP` is past every bound measured (3.12 to 3.14) and, as arrays, 200,000 bytes.

_UNREADABLE = "Invalid JSON: the document is nested too deeply or holds a number too long to read"
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
    assert sys.get_int_max_str_digits() == 4300
    # Past the bound on this interpreter, so _DEEP is a refusal, not a parse.
    assert _parser_depth_bound() + _DEPTH_MARGIN < _DEEP


# Whole documents the parser refuses; each answers the one fixed detail.
_UNREADABLE_DOCS = {
    "deep_arrays": lambda bound: _nest(_DEEP),
    "deep_just_over_the_bound": lambda bound: _nest(bound + _DEPTH_MARGIN),
    "deep_objects": lambda bound: '{"a": ' * _DEEP + "1" + "}" * _DEEP,
    "deep_beside_items": lambda bound: '{"items": [], "x": ' + _nest(_DEEP) + "}",
    "long_int": lambda bound: _DIGITS_OVER_LIMIT,
    "negative_long_int": lambda bound: "-" + _DIGITS_OVER_LIMIT,
    "long_int_nested_in_a_canvas": lambda bound: (
        '[{"name": "lab", "canvas": {"nodes": [{"id": "n", "data": {"x": -'
        + _DIGITS_OVER_LIMIT
        + '}}], "edges": []}}]'
    ),
    "long_int_then_deep": lambda bound: "[" + _DIGITS_OVER_LIMIT + ", " + _nest(_DEEP) + "]",
    "deep_then_long_int": lambda bound: "[" + _nest(_DEEP, _DIGITS_OVER_LIMIT) + ", 1]",
}


@pytest.mark.parametrize("kind", list(_UNREADABLE_DOCS))
def test_parse_json_topologies_refuses_an_unreadable_document_with_the_fixed_detail(kind):
    doc = _UNREADABLE_DOCS[kind](_parser_depth_bound())
    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(doc.encode())
    assert exc.value.status_code == 422
    assert exc.value.detail == _UNREADABLE


def test_parse_json_topologies_reads_a_document_just_under_the_depth_bound():
    depth = _parser_depth_bound() - _DEPTH_MARGIN
    items = parse_json_topologies(_nest(depth).encode())
    # Never compare the nested value itself: the comparison recurses as deep.
    assert isinstance(items, list)
    assert len(items) == 1


@pytest.mark.parametrize("sign", ["", "-"])
def test_parse_json_topologies_reads_an_integer_at_the_digit_limit(sign):
    items = parse_json_topologies(f"[{sign}{_DIGITS_AT_LIMIT}]".encode())
    assert items == [int(sign + _DIGITS_AT_LIMIT)]


def test_parse_json_topologies_keeps_the_decoder_position_for_a_syntax_error():
    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(b"[")
    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid JSON: Expecting value: line 1 column 2 (char 1)"


# Fragments the parser refuses, placed beside the items or inside the valid topology.
_UNREADABLE_FRAGMENTS = {
    "deep": lambda bound: _nest(_DEEP),
    "deep_just_over_the_bound": lambda bound: _nest(bound + _DEPTH_MARGIN),
    "long_int": lambda bound: _DIGITS_OVER_LIMIT,
    "negative_long_int": lambda bound: '[0, {"b": -' + _DIGITS_OVER_LIMIT + "}]",
    "long_int_then_deep": lambda bound: "[" + _DIGITS_OVER_LIMIT + ", " + _nest(_DEEP) + "]",
    "deep_then_long_int": lambda bound: _nest(_DEEP, _DIGITS_OVER_LIMIT),
}

_VALID_TOPOLOGY = {"name": "lab", "canvas": {"nodes": [], "edges": []}}


def _with_fragment(fragment: str, placement: str) -> bytes:
    row_json = json.dumps(_VALID_TOPOLOGY)
    if placement == "beside_items":
        return ('{"items": [' + row_json + '], "x": ' + fragment + "}").encode()
    return ('{"items": [' + row_json[:-1] + ', "extra": ' + fragment + "}]}").encode()


async def _topology_names(client) -> list[str]:
    return sorted(t["name"] for t in (await client.get("/topologies")).json()["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("fragment", list(_UNREADABLE_FRAGMENTS))
@pytest.mark.parametrize("placement", ["beside_items", "inside_a_row"])
async def test_an_unreadable_json_import_is_a_fixed_422_and_writes_nothing(
    user_client, dry_run, fragment, placement
):
    """Issue #1511: each of these answered 500 through the route, to a non-admin."""
    before = await _count_versions()
    body = _with_fragment(_UNREADABLE_FRAGMENTS[fragment](_parser_depth_bound()), placement)
    resp = await user_client.post(
        "/topologies/import",
        params={"format": "json", "dry_run": dry_run},
        files={"file": ("t.json", io.BytesIO(body), "application/json")},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": _UNREADABLE}
    assert await _count_versions() == before
    assert await _topology_names(user_client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fragment",
    [
        lambda bound: _nest(bound - _DEPTH_MARGIN),
        lambda bound: "[" + _DIGITS_AT_LIMIT + ", -" + _DIGITS_AT_LIMIT + "]",
    ],
    ids=["deep_just_under_the_bound", "ints_at_the_digit_limit"],
)
async def test_a_json_import_just_under_the_parser_bounds_imports(user_client, fragment):
    """The other side of each boundary: the same file shape parses and its topology lands."""
    body = _with_fragment(fragment(_parser_depth_bound()), "beside_items")
    resp = await user_client.post(
        "/topologies/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(body), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1, resp.text
    assert await _topology_names(user_client) == ["lab"]


# A JSON import item the importer cannot read (issue #1573) --------------------------
#
# An item that is not an object, a `name` that is not a string, or a canvas whose
# parts the device-name pass reads are the wrong type. The pass over every record's
# device names runs before any row, and the name is read before the row's `try`, so
# most of these answered 500 to any signed-in user (a bad name after the rows before
# it had committed), and the ones that are empty, zero, or false were read as absent;
# TOPO-BULK-21 lists which. Each is now a rejected row with a fixed reason and the
# rest of the batch runs (TOPO-BULK-18, TOPO-BULK-21).

_ROW_NOT_AN_OBJECT = "row is not an object"
_NAME_NOT_A_STRING = "name: must be a string"

_TOPO_NON_OBJECTS = {
    "empty_list": [],
    "list": ["lab"],
    "string": "lab",
    "empty_string": "",
    "int": 1,
    "zero": 0,
    "float": 1.5,
    "true": True,
    "false": False,
    "null": None,
}

_TOPO_NON_STRING_NAMES = {
    "int": 1,
    "zero": 0,
    "float": 1.5,
    "true": True,
    "false": False,
    "empty_list": [],
    "list": ["lab"],
    "empty_object": {},
    "object": {"name": "lab"},
}

_GOOD_TOPOLOGIES = [
    {"name": "lab-a", "canvas": {"nodes": [], "edges": []}},
    {"name": "lab-b", "canvas": {"nodes": [], "edges": []}},
]


def _placed(bad, position: str) -> tuple[list, int]:
    index = {"first": 0, "middle": 1, "last": 2}[position]
    items = list(_GOOD_TOPOLOGIES)
    items.insert(index, bad)
    return items, index


def _rows_with_reject(bad_index: int, reason: str, identity: str | None = None) -> list[dict]:
    names = iter(t["name"] for t in _GOOD_TOPOLOGIES)
    return [
        {"row": i, "action": "reject", "identity": identity, "reason": reason}
        if i == bad_index
        else {"row": i, "action": "create", "identity": next(names), "reason": None}
        for i in range(len(_GOOD_TOPOLOGIES) + 1)
    ]


async def _assert_good_rows_landed(client, dry_run: bool) -> None:
    expected = [] if dry_run else sorted(t["name"] for t in _GOOD_TOPOLOGIES)
    assert await _topology_names(client) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("kind", list(_TOPO_NON_OBJECTS))
@pytest.mark.parametrize("position", ["first", "middle", "last"])
async def test_an_item_that_is_not_an_object_is_a_rejected_row(
    user_client, dry_run, kind, position
):
    items, bad_index = _placed(_TOPO_NON_OBJECTS[kind], position)
    resp = await _import_json(user_client, items, dry_run=str(dry_run).lower())
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["rows"] == _rows_with_reject(bad_index, _ROW_NOT_AN_OBJECT)
    assert (report["total"], report["created"], report["rejected"]) == (3, 2, 1)
    await _assert_good_rows_landed(user_client, dry_run)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("kind", list(_TOPO_NON_STRING_NAMES))
async def test_an_item_whose_name_is_not_a_string_is_a_rejected_row(user_client, dry_run, kind):
    bad = {"name": _TOPO_NON_STRING_NAMES[kind], "canvas": {"nodes": [], "edges": []}}
    items, bad_index = _placed(bad, "middle")
    resp = await _import_json(user_client, items, dry_run=str(dry_run).lower())
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == _rows_with_reject(bad_index, _NAME_NOT_A_STRING)
    await _assert_good_rows_landed(user_client, dry_run)


def _device_node(name="switch-a", **overrides) -> dict:
    node = {"id": "n1", "data": {"device": {"name": name}, "label": "x"}}
    node.update(overrides)
    return node


# Each canvas a record may carry that the device-name pass could not read, and the
# fixed reason it is rejected with: `<path>: <message>`, the path naming the node by
# its index and never a value from the file.
_BAD_CANVASES = {
    "canvas_int": (1, "canvas: must be an object"),
    "canvas_zero": (0, "canvas: must be an object"),
    "canvas_string": ("x", "canvas: must be an object"),
    "canvas_empty_string": ("", "canvas: must be an object"),
    "canvas_list": ([{"nodes": []}], "canvas: must be an object"),
    "canvas_empty_list": ([], "canvas: must be an object"),
    "canvas_true": (True, "canvas: must be an object"),
    "nodes_string": ({"nodes": "n1"}, "canvas.nodes: must be a list"),
    "nodes_int": ({"nodes": 1}, "canvas.nodes: must be a list"),
    "nodes_object": ({"nodes": {"n1": {}}}, "canvas.nodes: must be a list"),
    "nodes_empty_object": ({"nodes": {}}, "canvas.nodes: must be a list"),
    "nodes_true": ({"nodes": True}, "canvas.nodes: must be a list"),
    "node_int": ({"nodes": [1]}, "canvas.nodes.0: must be an object"),
    "node_null": ({"nodes": [None]}, "canvas.nodes.0: must be an object"),
    "node_string": ({"nodes": ["n1"]}, "canvas.nodes.0: must be an object"),
    "node_list": ({"nodes": [[]]}, "canvas.nodes.0: must be an object"),
    "second_node_int": ({"nodes": [_device_node(), 1]}, "canvas.nodes.1: must be an object"),
    "last_of_three_nodes": (
        {"nodes": [_device_node(), {"id": "n2"}, "n3"]},
        "canvas.nodes.2: must be an object",
    ),
    "data_int": ({"nodes": [{"id": "n1", "data": 1}]}, "canvas.nodes.0.data: must be an object"),
    "data_string": (
        {"nodes": [{"id": "n1", "data": "x"}]},
        "canvas.nodes.0.data: must be an object",
    ),
    "data_list": (
        {"nodes": [{"id": "n1", "data": [{"device": {}}]}]},
        "canvas.nodes.0.data: must be an object",
    ),
    "data_false": (
        {"nodes": [{"id": "n1", "data": False}]},
        "canvas.nodes.0.data: must be an object",
    ),
    "device_int": (
        {"nodes": [{"id": "n1", "data": {"device": 1}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_string": (
        {"nodes": [{"id": "n1", "data": {"device": "switch-a"}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_list": (
        {"nodes": [{"id": "n1", "data": {"device": ["switch-a"]}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_empty_string": (
        {"nodes": [{"id": "n1", "data": {"device": ""}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_name_int": (
        {"nodes": [_device_node(name=1)]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_list": (
        {"nodes": [_device_node(name=["switch-a"])]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_object": (
        {"nodes": [_device_node(name={"a": "b"})]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_true": (
        {"nodes": [_device_node(name=True)]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "second_device_name_int": (
        {"nodes": [_device_node(), _device_node(name=2, id="n2")]},
        "canvas.nodes.1.data.device.name: must be a string",
    ),
    # Wrong-typed values that are falsy: each `is None` test must not become a
    # truthiness test, which would read them as absent.
    "canvas_false": (False, "canvas: must be an object"),
    "nodes_zero": ({"nodes": 0}, "canvas.nodes: must be a list"),
    "nodes_false": ({"nodes": False}, "canvas.nodes: must be a list"),
    "nodes_empty_string": ({"nodes": ""}, "canvas.nodes: must be a list"),
    "node_zero": ({"nodes": [0]}, "canvas.nodes.0: must be an object"),
    "node_false": ({"nodes": [False]}, "canvas.nodes.0: must be an object"),
    "node_empty_string": ({"nodes": [""]}, "canvas.nodes.0: must be an object"),
    "data_zero": ({"nodes": [{"data": 0}]}, "canvas.nodes.0.data: must be an object"),
    "data_empty_string": ({"nodes": [{"data": ""}]}, "canvas.nodes.0.data: must be an object"),
    "data_empty_list": ({"nodes": [{"data": []}]}, "canvas.nodes.0.data: must be an object"),
    "device_zero": (
        {"nodes": [{"data": {"device": 0}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_false": (
        {"nodes": [{"data": {"device": False}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_empty_list": (
        {"nodes": [{"data": {"device": []}}]},
        "canvas.nodes.0.data.device: must be an object",
    ),
    "device_name_zero": (
        {"nodes": [_device_node(name=0)]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_zero_float": (
        {"nodes": [_device_node(name=0.0)]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_false": (
        {"nodes": [_device_node(name=False)]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_empty_list": (
        {"nodes": [_device_node(name=[])]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    "device_name_empty_object": (
        {"nodes": [_device_node(name={})]},
        "canvas.nodes.0.data.device.name: must be a string",
    ),
    # A null or absent part early in the canvas, then a bad part in a later node:
    # passing the null part must move on to the next node, never end the check.
    "absent_data_then_bad_node": (
        {"nodes": [{"id": "n1"}, 1]},
        "canvas.nodes.1: must be an object",
    ),
    "null_data_then_bad_node": (
        {"nodes": [{"id": "n1", "data": None}, 1]},
        "canvas.nodes.1: must be an object",
    ),
    "absent_device_then_bad_node": (
        {"nodes": [{"id": "n1", "data": {"label": "x"}}, 1]},
        "canvas.nodes.1: must be an object",
    ),
    "null_device_then_bad_node": (
        {"nodes": [{"data": {"device": None}}, 1]},
        "canvas.nodes.1: must be an object",
    ),
    "null_device_name_then_bad_node": (
        {"nodes": [_device_node(name=None), 1]},
        "canvas.nodes.1: must be an object",
    ),
    "absent_device_name_then_bad_node": (
        {"nodes": [{"id": "n1", "data": {"device": {"role": "dut"}}}, 1]},
        "canvas.nodes.1: must be an object",
    ),
    "null_data_then_bad_data": (
        {"nodes": [{"id": "n1", "data": None}, {"id": "n2", "data": 1}]},
        "canvas.nodes.1.data: must be an object",
    ),
    "null_device_then_bad_device": (
        {"nodes": [{"data": {"device": None}}, {"data": {"device": "x"}}]},
        "canvas.nodes.1.data.device: must be an object",
    ),
    "null_device_then_bad_device_name": (
        {"nodes": [{"data": {"device": None}}, _device_node(name=0, id="n2")]},
        "canvas.nodes.1.data.device.name: must be a string",
    ),
    "null_device_name_then_bad_device_name": (
        {"nodes": [_device_node(name=None), _device_node(name=1, id="n2")]},
        "canvas.nodes.1.data.device.name: must be a string",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("kind", list(_BAD_CANVASES))
async def test_a_canvas_the_name_pass_cannot_read_is_a_rejected_row(user_client, dry_run, kind):
    canvas, reason = _BAD_CANVASES[kind]
    items, bad_index = _placed({"name": "  bad-lab  ", "canvas": canvas}, "middle")
    # Every device name in the good rows (none) and in the rejected record must stay
    # out of the one resolve call: the bad record is skipped by the name pass.
    with _resolver({}) as resolver:
        resp = await _import_json(user_client, items, dry_run=str(dry_run).lower())
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == _rows_with_reject(bad_index, reason, identity="bad-lab")
    resolver.assert_not_called()
    await _assert_good_rows_landed(user_client, dry_run)


@pytest.mark.asyncio
async def test_a_rejected_record_is_left_out_of_the_name_resolution(admin_client):
    """The bad record names a device in a readable node; the name pass skips the whole
    record, so only the good record's names are sent to inventory."""
    bad = {"name": "bad-lab", "canvas": {"nodes": [_device_node(name="sw-hidden"), 1]}}
    good = {"name": "good-lab", "canvas": {"nodes": [_device_node()], "edges": []}}
    with _resolver({"switch-a": DEV_A}) as resolver:
        resp = await _import_json(admin_client, [bad, good])
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"], r["reason"]) for r in resp.json()["rows"]] == [
        ("reject", "bad-lab", "canvas.nodes.1: must be an object"),
        ("create", "good-lab", None),
    ]
    resolver.assert_awaited_once_with(["switch-a"])


@pytest.mark.asyncio
async def test_a_non_admin_with_a_visibility_filter_gets_the_same_rejected_row(user_client):
    """The non-admin path with a real filter (an empty visible set) reaches the same
    shape check; the visibility read itself is unchanged."""
    with (
        patch(
            "app.services.bulk_service.resolve_caller_visibility",
            new=AsyncMock(return_value=set()),
        ),
        _resolver({}),
    ):
        resp = await _import_json(user_client, [1, {"name": "lab-a"}, {"name": 1}])
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"], r["reason"]) for r in resp.json()["rows"]] == [
        ("reject", None, _ROW_NOT_AN_OBJECT),
        ("create", "lab-a", None),
        ("reject", None, _NAME_NOT_A_STRING),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canvas",
    [
        None,
        {},
        {"nodes": None, "edges": []},
        {"nodes": [{"id": "n1"}], "edges": []},
        {"nodes": [{"id": "n1", "data": None}], "edges": []},
        {"nodes": [{"id": "n1", "data": {"device": None}}], "edges": []},
        {"nodes": [{"id": "n1", "data": {"device": {"name": None}}}], "edges": []},
        {"nodes": [{"id": "n1", "data": {"device": {}}}], "edges": []},
    ],
    ids=[
        "canvas_null",
        "canvas_empty",
        "nodes_null",
        "node_without_data",
        "data_null",
        "device_null",
        "device_name_null",
        "device_empty",
    ],
)
async def test_a_null_or_absent_part_passes_the_shape_check(user_client, canvas):
    resp = await _import_json(user_client, [{"name": "lab", "canvas": canvas}])
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [("create", "lab")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edges", "reason"),
    [
        ("e1", "canvas.edges: must be a list"),
        (1, "canvas.edges: must be a list"),
        ({"e1": {}}, "canvas.edges: must be a list"),
        ([1], "canvas.edges.0: must be an object"),
        ([None], "canvas.edges.0: must be an object"),
        (["e1"], "canvas.edges.0: must be an object"),
        ([{"id": "e1", "source": 1, "target": []}], "canvas.edges.0.source: must be a string"),
    ],
    ids=[
        "string",
        "int",
        "object",
        "edge_int",
        "edge_null",
        "edge_string",
        "edge_ends_not_strings",
    ],
)
async def test_an_edge_list_of_the_wrong_shape_is_a_rejected_row(user_client, edges, reason):
    """Edges are held to the same shape check as nodes (canvas_shape): a rejected
    row with a fixed reason, and the rest of the batch runs."""
    resp = await _import_json(
        user_client,
        [{"name": "lab", "canvas": {"nodes": [], "edges": edges}}, {"name": "lab-2"}],
    )
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"], r["reason"]) for r in resp.json()["rows"]] == [
        ("reject", "lab", reason),
        ("create", "lab-2", None),
    ]


@pytest.mark.asyncio
async def test_a_committing_import_reports_the_topology_before_a_bad_item(user_client):
    """The issue's partial-write case: `[{"name": "T-ok"}, {"name": 1}]` wrote `T-ok`
    and answered 500 with no report. The report now says so."""
    resp = await _import_json(user_client, [{"name": "T-ok"}, {"name": 1}])
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [
        ("create", "T-ok"),
        ("reject", None),
    ]
    assert await _topology_names(user_client) == ["T-ok"]


_NOT_TOPOLOGIES = "JSON import must be a list of topologies or an object with an 'items' list"


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (1, _NOT_TOPOLOGIES),
        (1.5, _NOT_TOPOLOGIES),
        ("lab", _NOT_TOPOLOGIES),
        (True, _NOT_TOPOLOGIES),
        (None, _NOT_TOPOLOGIES),
        ({}, _NOT_TOPOLOGIES),
        ({"topologies": []}, _NOT_TOPOLOGIES),
        ({"items": 1}, "'items' must be a list"),
        ({"items": "lab"}, "'items' must be a list"),
        ({"items": None}, "'items' must be a list"),
        ({"items": {"name": "lab"}}, "'items' must be a list"),
    ],
    ids=[
        "int",
        "float",
        "string",
        "bool",
        "null",
        "empty_object",
        "object_without_items",
        "items_int",
        "items_string",
        "items_null",
        "items_object",
    ],
)
async def test_a_document_that_is_not_the_expected_container_is_a_422(
    user_client, dry_run, body, detail
):
    resp = await _import_json(user_client, body, dry_run=str(dry_run).lower())
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": detail}
    assert await _topology_names(user_client) == []


# A CSV import the csv module cannot read (issue #1574) ------------------------------

_DEFAULT_FIELD_LIMIT = 131_072
_CSV_HEADER = "topology_name,source_device,source_port,target_device,target_port,layer"


async def _import_csv(client, text: str, *, dry_run: bool = False):
    return await client.post(
        "/topologies/import",
        params={"format": "csv", "dry_run": str(dry_run).lower()},
        files={"file": ("t.csv", io.BytesIO(text.encode()), "text/csv")},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [_DEFAULT_FIELD_LIMIT, _DEFAULT_FIELD_LIMIT + 1])
async def test_a_device_cell_past_the_field_limit_is_judged_not_a_500(user_client, length):
    device = "d" * length
    with _resolver({}):
        resp = await _import_csv(user_client, f"{_CSV_HEADER}\nlab,{device},,switch-b,,\n")
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["rows"]
    assert (row["action"], row["identity"]) == ("reject", "lab")
    assert row["reason"] == f"unresolved device names: {device}, switch-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_topology_name_past_the_field_limit_is_read(user_client, dry_run):
    name = "t" * (_DEFAULT_FIELD_LIMIT + 1)
    resp = await _import_csv(user_client, f"{_CSV_HEADER}\n{name},,,,,\n", dry_run=dry_run)
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [("create", name)]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
@pytest.mark.parametrize("dry_run", [True, False])
async def test_every_line_ending_imports_the_same_topologies(user_client, ending, dry_run):
    text = ending.join([_CSV_HEADER, "lab-a,,,,,", "lab-b,,,,,"]) + ending
    resp = await _import_csv(user_client, text, dry_run=dry_run)
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [
        ("create", "lab-a"),
        ("create", "lab-b"),
    ]
    assert await _topology_names(user_client) == ([] if dry_run else ["lab-a", "lab-b"])


@pytest.mark.asyncio
async def test_a_cr_only_csv_with_edges_imports_its_wiring(admin_client):
    """A whole file with classic Mac line endings: each line is an edge row."""
    await _seed_connection()
    text = "\r".join([_CSV_HEADER, "lab,switch-a,eth0,switch-b,eth0,L1"]) + "\r"
    with _resolver({"switch-a": DEV_A, "switch-b": DEV_B}):
        resp = await _import_csv(admin_client, text)
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [("create", "lab")]
    exported = await admin_client.get("/topologies/export", params={"format": "csv"})
    assert exported.text.splitlines()[1] == "lab,switch-a,eth0,switch-b,eth0,L1"


@pytest.mark.asyncio
async def test_a_bare_cr_in_an_unquoted_cell_ends_the_line(user_client):
    resp = await _import_csv(user_client, f"{_CSV_HEADER}\r\nlab-a\rlab-b,,,,,\r\n")
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [
        ("create", "lab-a"),
        ("create", "lab-b"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "rows"),
    [
        (f"{_CSV_HEADER}\nla\x00b,,,,,\n", 1),
        (f'{_CSV_HEADER}\n"lab,,,,,\n', 1),
        (f'{_CSV_HEADER}\nla"b,,,,,\n', 1),
        (f'{_CSV_HEADER}\n"la"b,,,,,\n', 1),
        (f"{_CSV_HEADER}\nlab,,,,,,extra\n", 1),
        ("", 0),
        (f"{_CSV_HEADER}\n", 0),
    ],
    ids=[
        "nul",
        "unterminated_quote",
        "quote_inside_unquoted_cell",
        "text_after_a_closing_quote",
        "extra_cells",
        "empty_file",
        "header_only",
    ],
)
async def test_csv_the_module_reads_leniently_is_judged_not_a_500(user_client, text, rows):
    resp = await _import_csv(user_client, text)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["total"] == len(report["rows"]) == rows


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    ("text", "line"),
    [
        (f"{_CSV_HEADER},a_very_long_extra_column\nlab,,,,,\n", 1),
        (f"{_CSV_HEADER}\nlab-a,,,,,\nlab-b,,,,,\nlab-c-is-too-long,,,,,\n", 4),
        (f'{_CSV_HEADER}\n"lab\r\nx",,,,,\nlab-c-is-too-long,,,,,\n', 4),
        (f"{_CSV_HEADER}\rlab-a,,,,,\rlab-c-is-too-long,,,,,\r", 3),
    ],
    ids=["header", "fourth_line", "after_a_multiline_cell", "cr_lines"],
)
async def test_a_csv_the_module_refuses_is_a_fixed_422_and_writes_nothing(
    user_client, monkeypatch, dry_run, text, line
):
    """The csv module is made to refuse a real cell by holding its field limit at 13
    characters (the longest header cell, `topology_name`); its own `csv.Error` then
    reaches the route. The rows before the refused line are not written either."""
    from herd_common import csv_import

    monkeypatch.setattr(csv_import, "_field_limit_for", lambda text: 13)
    resp = await _import_csv(user_client, text, dry_run=dry_run)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": f"Invalid CSV: line {line} could not be read; fix it and retry"
    }
    assert await _topology_names(user_client) == []
    assert await _count_versions() == 0


# The parse and the rendering run off the event loop (issue #1417) --------------------
#
# The import's decode, parse, surrogate scan, shape check, and device-name collection
# (`_read_import`), and the export's records and body (`render_export`), run on a bulk
# worker thread (`herd_common.bulk_work.run_bulk_work`). Each overlap is staged with
# events, never a sleep: the parse blocks on a `threading.Event` the test sets, and the
# test waits for its signal off the loop.

_OFF_LOOP_WAIT = 10.0


async def _signalled(event: threading.Event) -> bool:
    return await asyncio.to_thread(event.wait, _OFF_LOOP_WAIT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "parser", "body"),
    [
        ("json", "parse_json_topologies", b"[]"),
        ("csv", "parse_csv_topologies", (_CSV_HEADER + "\n").encode()),
    ],
)
async def test_another_request_is_served_while_an_import_parse_is_held(
    user_client, monkeypatch, fmt, parser, body
):
    """The parse runs on a thread that is not the loop's, and while it is held the
    service answers another request. Mutation-checked: calling `_read_import` directly
    in `import_topologies` (on the loop) fails this."""
    from app.services import bulk_service

    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    parse_threads: list[int] = []
    real_parse = getattr(bulk_service, parser)

    def held_parse(raw):
        parse_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(_OFF_LOOP_WAIT), "the parse was never released"
        return real_parse(raw)

    monkeypatch.setattr(bulk_service, parser, held_parse)
    importing = asyncio.create_task(
        user_client.post(
            "/topologies/import",
            params={"format": fmt},
            files={"file": (f"t.{fmt}", io.BytesIO(body), "text/plain")},
        )
    )
    try:
        assert await _signalled(entered), "the parse never started"
        health = await user_client.get("/health")
        assert health.status_code == 200
        assert not importing.done()
    finally:
        release.set()
    resp = await importing
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
    assert parse_threads and parse_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_the_device_names_are_collected_with_the_parse(user_client):
    """The names `_read_import` collects off the loop are the ones the resolver is
    asked for, and a record rejected for its shape adds none."""
    items = [
        {"name": "lab", "canvas": _canvas_with_names()},
        {"name": "bad", "canvas": {"nodes": "not-a-list"}},
    ]
    with _resolver({}) as resolve:
        resp = await _import_json(user_client, items, dry_run="true")
    assert resp.status_code == 200, resp.text
    resolve.assert_awaited_once_with(["switch-a", "switch-b"])
    assert [(r["identity"], r["action"]) for r in resp.json()["rows"]] == [
        ("lab", "reject"),
        ("bad", "reject"),
    ]


@pytest.mark.asyncio
async def test_each_record_is_judged_once_with_the_parse(user_client, monkeypatch):
    """Each record's shape reason is computed once, on the parse's worker thread,
    and the row loop rejects by that reason without judging the canvas again."""
    from app.services import bulk_service

    loop_thread = threading.get_ident()
    calls: list[int] = []
    real_reason = bulk_service._record_shape_reason
    real_problem = bulk_service.canvas_problem

    def recording_reason(rec):
        calls.append(threading.get_ident())
        return real_reason(rec)

    def recording_problem(canvas):
        calls.append(threading.get_ident())
        return real_problem(canvas)

    monkeypatch.setattr(bulk_service, "_record_shape_reason", recording_reason)
    monkeypatch.setattr(bulk_service, "canvas_problem", recording_problem)
    items = [
        {"name": "lab", "canvas": {"nodes": [{"id": "n1"}], "edges": []}},
        {"name": "bad", "canvas": {"nodes": "not-a-list"}},
        {"name": "other"},
    ]
    with _resolver({}):
        resp = await _import_json(user_client, items, dry_run="true")
    assert resp.status_code == 200, resp.text
    assert [(r["identity"], r["action"], r["reason"]) for r in resp.json()["rows"]] == [
        ("lab", "create", None),
        ("bad", "reject", "canvas.nodes: must be a list"),
        ("other", "create", None),
    ]
    # One reason and one canvas check per record, none on the loop's thread.
    assert len(calls) == 2 * len(items)
    assert loop_thread not in calls


class _RefusingExecutor(concurrent.futures.ThreadPoolExecutor):
    """A default executor that fails any work handed to it."""

    def submit(self, *args, **kwargs):
        raise AssertionError("work was handed to the default executor")


@pytest.mark.asyncio
async def test_the_import_never_takes_the_default_executor(user_client):
    """The default executor is the one `loop.getaddrinfo` resolves names on (issue
    #1416)."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(_RefusingExecutor())
    try:
        resp = await _import_json(user_client, [{"name": "lab"}])
    finally:
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor())
    assert resp.status_code == 200, resp.text
    assert [(r["identity"], r["action"]) for r in resp.json()["rows"]] == [("lab", "create")]


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["csv", "json"])
async def test_an_export_is_rendered_off_the_loop(admin_client, monkeypatch, fmt):
    """The body is the one the renderer gives, built on a thread that is not the
    loop's. Mutation-checked: calling `render_export` directly in the route fails
    this."""
    from app.routes import bulk as bulk_router

    await _seed_topology("lab", ADMIN_ID, _canvas_with_ids())
    expected = await admin_client.get("/topologies/export", params={"format": fmt})
    assert expected.status_code == 200
    loop_thread = threading.get_ident()
    render_threads: list[int] = []
    real = bulk_router.render_export

    def recording(*args):
        render_threads.append(threading.get_ident())
        return real(*args)

    monkeypatch.setattr(bulk_router, "render_export", recording)
    resp = await admin_client.get("/topologies/export", params={"format": fmt})
    assert resp.status_code == 200
    assert resp.content == expected.content
    assert resp.headers["content-type"] == expected.headers["content-type"]
    assert resp.headers["content-disposition"] == expected.headers["content-disposition"]
    assert render_threads and render_threads[0] != loop_thread
    assert "lab" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["csv", "json"])
async def test_an_export_holds_no_connection_while_it_renders(admin_client, monkeypatch, fmt):
    """While the render is held on its thread, the request has given its pooled
    connection back, the topologies it renders are still readable (closed, not
    expired), and the body is the one the render gives. Mutation-checked: removing
    the `await db.close()` from `export_topologies` fails this."""
    from app.routes import bulk as bulk_router
    from sqlalchemy import event

    await _seed_topology("lab", ADMIN_ID, _canvas_with_ids())
    expected = await admin_client.get("/topologies/export", params={"format": fmt})
    assert expected.status_code == 200
    entered = threading.Event()
    release = threading.Event()
    real = bulk_router.render_export
    count = {"out": 0, "seen": 0}

    def checkout(*_args):
        count["out"] += 1
        count["seen"] += 1

    def checkin(*_args):
        count["out"] -= 1

    def held(*args):
        entered.set()
        assert release.wait(_OFF_LOOP_WAIT), "the render was never released"
        return real(*args)

    monkeypatch.setattr(bulk_router, "render_export", held)
    pool = test_engine.sync_engine.pool
    event.listen(pool, "checkout", checkout)
    event.listen(pool, "checkin", checkin)
    try:
        exporting = asyncio.create_task(
            admin_client.get("/topologies/export", params={"format": fmt})
        )
        try:
            assert await _signalled(entered), "the render never started"
            held_out = count["out"]
        finally:
            release.set()
        resp = await exporting
    finally:
        event.remove(pool, "checkout", checkout)
        event.remove(pool, "checkin", checkin)
    assert count["seen"] > 0
    assert held_out == 0
    assert resp.status_code == 200
    assert resp.content == expected.content
    assert "lab" in resp.text


# However the upload is sent, the file bound holds (issue #1417) ------------------------


def _multipart(file_bytes: bytes) -> tuple[bytes, dict[str, str]]:
    """The multipart body httpx builds for one file, and its content type."""
    request = httpx.Request(
        "POST", "http://test/", files={"file": ("t.json", file_bytes, "application/json")}
    )
    return request.read(), {"content-type": request.headers["content-type"]}


async def _chunks(body: bytes, size: int = 4096):
    for start in range(0, len(body), size):
        yield body[start : start + size]


@pytest.mark.asyncio
async def test_a_streamed_upload_with_no_length_is_held_to_the_exact_file_bound(
    user_client, monkeypatch
):
    """Sent chunked, with no Content-Length, a file at the bound is read and one a
    byte over it is the 413 naming the bound."""
    from app.routes import bulk as bulk_router

    file_bytes = b"[" + b" " * 9000 + b"]"
    body, headers = _multipart(file_bytes)

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(file_bytes))
    at = await user_client.post(
        "/topologies/import", params={"format": "json"}, content=_chunks(body), headers=headers
    )
    assert at.status_code == 200, at.text
    assert at.json()["total"] == 0

    monkeypatch.setattr(bulk_router, "IMPORT_UPLOAD_BYTES", len(file_bytes) - 1)
    over = await user_client.post(
        "/topologies/import", params={"format": "json"}, content=_chunks(body), headers=headers
    )
    assert over.status_code == 413, over.text
    assert over.json() == {
        "detail": [
            {
                "type": "body_too_large",
                "loc": ["body"],
                "msg": f"Request body is larger than {len(file_bytes) - 1} bytes",
            }
        ]
    }
