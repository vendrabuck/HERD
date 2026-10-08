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

import csv
import io
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models.connection import Connection
from app.models.topology import Topology, TopologyVersion
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
