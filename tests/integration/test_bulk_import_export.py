"""Integration tests for bulk import/export across the running stack.

These exercise the real gateway routes (/api/inventory/* and /api/cabling/*),
the inventory resolve-by-name internal endpoint reached via the cabling import,
and the existing topology validator. They self-seed via the session fixtures in
conftest.py and clean up what they create.

Requires a running stack (`make test-integration`). Not run in the stack-free
unit tier.
"""

import io
import json
import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_devices_export_json_carries_template_name(admin_client, fresh_device):
    resp = await admin_client.get("/inventory/devices/export", params={"format": "json"})
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["resource"] == "devices"
    match = [d for d in doc["items"] if d["name"] == fresh_device["name"]]
    assert match, "exported devices should include the freshly seeded device"
    assert match[0]["template_name"]
    assert "template_id" not in match[0]


@pytest.mark.asyncio
async def test_devices_export_csv_content_type(admin_client, fresh_device):
    resp = await admin_client.get("/inventory/devices/export", params={"format": "csv"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "name,template_name,topology_type" in resp.text.splitlines()[0]


@pytest.mark.asyncio
async def test_device_import_dry_run_writes_nothing(admin_client, dut_template):
    name = f"int-bulk-dry-{uuid.uuid4().hex[:8]}"
    items = [
        {
            "name": name,
            "template_name": dut_template["name"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        }
    ]
    resp = await admin_client.post(
        "/inventory/devices/import",
        params={"format": "json", "dry_run": "true"},
        files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["dry_run"] is True
    assert report["created"] == 1
    # Confirm nothing was written.
    listing = await admin_client.get("/inventory/devices", params={"search": name, "limit": 500})
    assert all(d["name"] != name for d in listing.json()["items"])


@pytest.mark.asyncio
async def test_device_import_commit_and_per_row_report(admin_client, dut_template):
    good = f"int-bulk-good-{uuid.uuid4().hex[:8]}"
    items = [
        {
            "name": good,
            "template_name": dut_template["name"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
        {
            "name": f"int-bulk-bad-{uuid.uuid4().hex[:8]}",
            "template_name": "no-such-template-xyz",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {},
        },
    ]
    created_id = None
    try:
        resp = await admin_client.post(
            "/inventory/devices/import",
            params={"format": "json"},
            files={"file": ("d.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()
        assert report["created"] == 1
        assert report["rejected"] == 1
        listing = await admin_client.get(
            "/inventory/devices", params={"search": good, "limit": 500}
        )
        match = [d for d in listing.json()["items"] if d["name"] == good]
        assert match, "the good row should have been created despite the bad row"
        created_id = match[0]["id"]
    finally:
        if created_id:
            await admin_client.delete(f"/inventory/devices/{created_id}")


@pytest.mark.asyncio
async def test_resolve_by_name_internal_requires_token(base_url):
    """The internal resolve-by-name endpoint is token-guarded (no JWT path)."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/inventory/devices/resolve-by-name",
            json={"names": ["anything"]},
            headers={"X-Internal-Token": "definitely-wrong"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_topology_import_rejects_unreachable_edge(admin_client, fresh_devices):
    """An imported topology whose edge has no physical path is rejected by the
    existing validator, exactly as an interactively drawn one would be."""
    devices = await fresh_devices(2)
    a, b = devices[0], devices[1]
    # No cabling connection exists between a and b, so the edge is unreachable.
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"name": a["name"]}, "label": a["name"]}},
            {"id": "n2", "data": {"device": {"name": b["name"]}, "label": b["name"]}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
    }
    items = [{"name": f"int-bulk-topo-{uuid.uuid4().hex[:8]}", "canvas": canvas}]
    resp = await admin_client.post(
        "/cabling/topologies/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["rejected"] == 1
    assert "validation failed" in report["rows"][0]["reason"]


@pytest.mark.asyncio
async def test_topology_import_rejects_unknown_device_name(admin_client):
    """A canvas referencing a device name that does not exist in this instance's
    inventory is rejected with an unresolved-name reason."""
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"name": f"ghost-{uuid.uuid4().hex}"}, "label": "g"}},
        ],
        "edges": [],
    }
    items = [{"name": f"int-bulk-ghost-{uuid.uuid4().hex[:8]}", "canvas": canvas}]
    resp = await admin_client.post(
        "/cabling/topologies/import",
        params={"format": "json"},
        files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["rejected"] == 1
    assert "unresolved device names" in report["rows"][0]["reason"]


@pytest.mark.asyncio
async def test_topology_export_roundtrips_isolated_node(admin_client, fresh_device):
    """A topology with a single device node (no edges) exports and re-imports,
    resolving the device by name on the way back in."""
    name = f"int-bulk-rt-{uuid.uuid4().hex[:8]}"
    create = await admin_client.post("/cabling/topologies", json={"name": name})
    assert create.status_code == 201, create.text
    tid = create.json()["id"]
    canvas = {
        "nodes": [
            {
                "id": "n1",
                "data": {
                    "device": {"id": fresh_device["id"], "name": fresh_device["name"]},
                    "label": fresh_device["name"],
                },
            }
        ],
        "edges": [],
    }
    imported_id = None
    try:
        upd = await admin_client.put(f"/cabling/topologies/{tid}", json={"canvas_data": canvas})
        assert upd.status_code == 200, upd.text

        exported = await admin_client.get("/cabling/topologies/export", params={"format": "json"})
        assert exported.status_code == 200
        items = [t for t in exported.json()["items"] if t["name"] == name]
        assert items, "export should include the created topology"
        node = items[0]["canvas"]["nodes"][0]
        assert node["data"]["device"]["name"] == fresh_device["name"]
        assert "id" not in node["data"]["device"]

        resp = await admin_client.post(
            "/cabling/topologies/import",
            params={"format": "json"},
            files={
                "file": (
                    "t.json",
                    io.BytesIO(json.dumps(items).encode()),
                    "application/json",
                )
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] == 1
        listing = await admin_client.get("/cabling/topologies", params={"limit": 500})
        dupes = [t for t in listing.json()["items"] if t["name"] == name]
        # Two now exist: the original and the imported copy.
        assert len(dupes) >= 2
        imported_id = next(t["id"] for t in dupes if t["id"] != tid)
    finally:
        await admin_client.delete(f"/cabling/topologies/{tid}")
        if imported_id:
            await admin_client.delete(f"/cabling/topologies/{imported_id}")
