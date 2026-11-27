"""Integration tests for bulk import/export across the running stack.

These exercise the real gateway routes (/api/inventory/* and /api/cabling/*),
the inventory resolve-by-name internal endpoint reached via the cabling import,
and the existing topology validator. They self-seed via the session fixtures in
conftest.py and clean up what they create.

Requires a running stack (`make test-integration`). Not run in the stack-free
unit tier.
"""

import csv
import io
import json
import uuid

import httpx
import pytest

from ._device_teardown import delete_device_checked
from ._topology_teardown import delete_topology_checked


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
            await delete_device_checked(admin_client, created_id)


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
    # The exact reason (issue #1147): "validation failed" alone matched every
    # validator refusal, not the unreachable edge this test builds.
    assert report["rows"][0]["reason"] == "topology validation failed: no_path(e1)", report


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
    resolving the device by name on the way back in. Re-importing the exported
    file updates the original in place by name rather than creating a duplicate
    (issue #336)."""
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
        report = resp.json()
        # Update-by-name: the existing topology is updated, not duplicated.
        assert report["updated"] == 1
        assert report["created"] == 0
        listing = await admin_client.get("/cabling/topologies", params={"limit": 500})
        dupes = [t for t in listing.json()["items"] if t["name"] == name]
        assert len(dupes) == 1, "re-import must update the original, not duplicate it"
        assert dupes[0]["id"] == tid
    finally:
        await delete_topology_checked(admin_client, tid)


@pytest.mark.asyncio
async def test_topology_csv_roundtrip_preserves_chosen_ports(admin_client, fresh_devices):
    """Issue #1006: CSV export writes the ports the editor stored (never a React Flow
    handle id), and importing that CSV writes them back where the fork-save resolver
    reads them, so a round trip keeps every port assignment."""
    a, b = await fresh_devices(2)
    for port in ("eth1", "eth2"):
        cable = await admin_client.post(
            "/cabling/connections",
            json={
                "device_a_id": a["id"],
                "port_a": port,
                "device_b_id": b["id"],
                "port_b": port,
                "connection_type": "L1",
            },
        )
        assert cable.status_code == 201, cable.text
    name = f"int-bulk-ports-{uuid.uuid4().hex[:8]}"
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": a["id"], "name": a["name"]}}},
            {"id": "n2", "data": {"device": {"id": b["id"], "name": b["name"]}}},
        ],
        "edges": [
            {
                "id": f"e-{port}",
                "source": "n1",
                "target": "n2",
                "sourceHandle": "right",
                "targetHandle": "left",
                "data": {"layer": "L1", "source_port_name": port, "target_port_name": port},
            }
            for port in ("eth1", "eth2")
        ],
    }
    create = await admin_client.post("/cabling/topologies", json={"name": name})
    assert create.status_code == 201, create.text
    tid = create.json()["id"]
    try:
        upd = await admin_client.put(f"/cabling/topologies/{tid}", json={"canvas_data": canvas})
        assert upd.status_code == 200, upd.text

        exported = await admin_client.get("/cabling/topologies/export", params={"format": "csv"})
        assert exported.status_code == 200
        rows = [r for r in csv.DictReader(io.StringIO(exported.text)) if r["topology_name"] == name]
        assert [(r["source_port"], r["target_port"]) for r in rows] == [
            ("eth1", "eth1"),
            ("eth2", "eth2"),
        ]

        body = "topology_name,source_device,source_port,target_device,target_port,layer\n"
        body += "".join(
            f"{name},{r['source_device']},{r['source_port']},{r['target_device']},"
            f"{r['target_port']},{r['layer']}\n"
            for r in rows
        )
        resp = await admin_client.post(
            "/cabling/topologies/import",
            params={"format": "csv"},
            files={"file": ("t.csv", io.BytesIO(body.encode()), "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated"] == 1, resp.text

        detail = await admin_client.get(f"/cabling/topologies/{tid}")
        edges = detail.json()["canvas_data"]["edges"]
        assert [
            (e["data"].get("source_port_name"), e["data"].get("target_port_name")) for e in edges
        ] == [("eth1", "eth1"), ("eth2", "eth2")]
    finally:
        await delete_topology_checked(admin_client, tid)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", ["true", "false"])
@pytest.mark.parametrize("fragment", ["deep", "long_int"])
async def test_topology_import_the_parser_refuses_is_a_fixed_422_for_a_non_admin(
    user_client, dry_run, fragment
):
    """Issue #1511: a JSON import nested past the parser's depth bound (100,000 levels,
    past every bound measured, 3.12 to 3.14) or holding a 4,301-digit integer answered
    500 through the gateway to any signed-in user. It is a 422 with a fixed detail, and
    nothing is written. The file is 200 KB at most, under the import's upload bound.

    The cleanup deletes a topology the import should never have written; when the test
    has already failed, a failing cleanup is added to that failure as a note instead of
    replacing it."""
    name = f"int-bulk-unreadable-{uuid.uuid4().hex[:8]}"
    bad = "[" * 100_000 + "]" * 100_000 if fragment == "deep" else "1" * 4301
    row = json.dumps({"name": name, "canvas": {"nodes": [], "edges": []}})
    body = ('{"items": [' + row + '], "x": ' + bad + "}").encode()
    failure: BaseException | None = None
    try:
        resp = await user_client.post(
            "/cabling/topologies/import",
            params={"format": "json", "dry_run": dry_run},
            files={"file": ("t.json", io.BytesIO(body), "application/json")},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json() == {
            "detail": (
                "Invalid JSON: the document is nested too deeply or holds a number too long to read"
            )
        }
        listing = await user_client.get("/cabling/topologies", params={"limit": 500})
        assert listing.status_code == 200, listing.text
        assert all(t["name"] != name for t in listing.json()["items"])
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            listing = await user_client.get("/cabling/topologies", params={"limit": 500})
            assert listing.status_code == 200, listing.text
            for leaked in [t for t in listing.json()["items"] if t["name"] == name]:
                await delete_topology_checked(user_client, leaked["id"])
        except Exception as cleanup_exc:
            if failure is None:
                raise
            failure.add_note(f"cleanup after this failure also failed: {cleanup_exc!r}")


async def _delete_topologies_named(client, name: str, failure: BaseException | None) -> None:
    """Delete every topology called `name`; a cleanup failure after a test failure is
    added to that failure as a note instead of replacing it."""
    try:
        listing = await client.get("/cabling/topologies", params={"limit": 500})
        assert listing.status_code == 200, listing.text
        for leaked in [t for t in listing.json()["items"] if t["name"] == name]:
            await delete_topology_checked(client, leaked["id"])
    except Exception as cleanup_exc:
        if failure is None:
            raise
        failure.add_note(f"cleanup after this failure also failed: {cleanup_exc!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", ["true", "false"])
async def test_topology_import_item_that_is_not_an_object_is_a_rejected_row_for_a_non_admin(
    user_client, dry_run
):
    """Issue #1573: an item that is not an object, or a `name` of 1, answered 500
    through the gateway to any signed-in user. Each is a rejected row with a fixed
    reason, and the good row is judged on its own."""
    name = f"int-bulk-shape-{uuid.uuid4().hex[:8]}"
    items = [1, {"name": name, "canvas": {"nodes": [], "edges": []}}, {"name": 1}]
    failure: BaseException | None = None
    try:
        resp = await user_client.post(
            "/cabling/topologies/import",
            params={"format": "json", "dry_run": dry_run},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
        assert resp.status_code == 200, resp.text
        assert [(r["action"], r["identity"], r["reason"]) for r in resp.json()["rows"]] == [
            ("reject", None, "row is not an object"),
            ("create", name, None),
            ("reject", None, "name: must be a string"),
        ]
        listing = await user_client.get("/cabling/topologies", params={"limit": 500})
        assert listing.status_code == 200, listing.text
        landed = [t["name"] for t in listing.json()["items"] if t["name"] == name]
        assert landed == ([] if dry_run == "true" else [name])
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await _delete_topologies_named(user_client, name, failure)


@pytest.mark.asyncio
async def test_device_import_item_that_is_not_an_object_is_a_rejected_row(admin_client):
    """Issue #1573 on inventory: a dry run, so nothing needs cleaning up."""
    resp = await admin_client.post(
        "/inventory/devices/import",
        params={"format": "json", "dry_run": "true"},
        files={"file": ("d.json", io.BytesIO(b'[1, null, {"name": 1}]'), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    assert [(r["action"], r["reason"]) for r in resp.json()["rows"]] == [
        ("reject", "row is not an object"),
        ("reject", "row is not an object"),
        ("reject", "name: must be a string"),
    ]


@pytest.mark.asyncio
async def test_topology_import_of_a_cr_only_csv_imports_for_a_non_admin(user_client):
    """Issue #1574: a CSV with classic Mac line endings (bare carriage returns)
    answered 500 through the gateway. Every line is now read as a row."""
    name = f"int-bulk-cr-{uuid.uuid4().hex[:8]}"
    body = f"topology_name,source_device,source_port,target_device,target_port,layer\r{name},,,,,\r"
    failure: BaseException | None = None
    try:
        resp = await user_client.post(
            "/cabling/topologies/import",
            params={"format": "csv"},
            files={"file": ("t.csv", io.BytesIO(body.encode()), "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        assert [(r["action"], r["identity"]) for r in resp.json()["rows"]] == [("create", name)]
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await _delete_topologies_named(user_client, name, failure)
