"""Playwright e2e: the topology editor saves a canvas larger than the default body limit
(issue #1484, OPS-VALID-10).

Since #1484 every route takes at most `MAX_REQUEST_BODY_BYTES` (256 KiB by default)
unless it is marked as a large-JSON route, which adds 4 MiB; the canvas save
`PUT /api/cabling/topologies/{id}` is one. A browser Save of a canvas over the default
must therefore still succeed. This test seeds a topology through the API with a
generated canvas of network element nodes (stored as sent, and not tied to any
device, so the test needs no seeded inventory), opens it in the editor, clicks Save,
and asserts the editor's own PUT carried more than the default and answered 200, then
reads the canvas back.

Self-seeding: one topology under a per-run name, no device, cable, port, or
reservation, so teardown deletes the topology and checks it is gone.
"""

import json
import os
import uuid

from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 30_000
DEFAULT_LIMIT = int(os.getenv("MAX_REQUEST_BODY_BYTES") or 256 * 1024)
ELEMENTS = 300


def _large_canvas() -> dict:
    nodes = [
        {
            "id": f"n-element-{i}",
            "type": "networkElementNode",
            "position": {"x": 160 * (i % 20), "y": 120 * (i // 20)},
            "data": {
                "element": {
                    "id": str(uuid.uuid4()),
                    "element_type": "vlan_segment",
                    "label": f"VLAN {100 + i}",
                    "attrs": {"description": f"lab segment {i} " + "for the body limit " * 60},
                }
            },
        }
        for i in range(ELEMENTS)
    ]
    return {"nodes": nodes, "edges": []}


def test_the_editor_saves_a_canvas_over_the_default_body_limit(pw_page):
    pw_login(pw_page)
    canvas = _large_canvas()
    seeded = len(json.dumps({"canvas_data": canvas}, separators=(",", ":")))
    assert seeded > DEFAULT_LIMIT, seeded
    topology_id = None
    try:
        topology = pw_api(
            pw_page, "POST", "/cabling/topologies", json={"name": f"pw1484-{uuid.uuid4().hex[:8]}"}
        ).json()
        topology_id = topology["id"]
        # The seeding PUT is itself a body over the default to the large-JSON route.
        pw_api(pw_page, "PUT", f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})

        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology_id}")
        expect(pw_page.locator(".react-flow")).to_be_visible(timeout=WAIT_MS)
        expect(pw_page.locator('[data-id="n-element-0"]')).to_be_visible(timeout=WAIT_MS)
        save = pw_page.get_by_role("button", name="Save", exact=True)
        expect(save).to_be_enabled(timeout=WAIT_MS)

        with pw_page.expect_response(
            lambda r: (
                r.request.method == "PUT"
                and r.url.endswith(f"/api/cabling/topologies/{topology_id}")
            ),
            timeout=WAIT_MS,
        ) as put:
            save.click()
        sent = put.value.request.post_data_buffer or b""
        assert len(sent) > DEFAULT_LIMIT, len(sent)
        assert put.value.status == 200, put.value.status
        expect(pw_page.get_by_text("Topology saved")).to_be_visible(timeout=WAIT_MS)

        saved = pw_api(pw_page, "GET", f"/cabling/topologies/{topology_id}").json()
        ids = {n["id"] for n in saved["canvas_data"]["nodes"]}
        assert ids == {n["id"] for n in canvas["nodes"]}
    finally:
        if topology_id:
            resp = pw_api(
                pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True
            )
            if resp.status_code not in (204, 404):
                log_cleanup_failure("topology", topology_id, resp)
            gone = pw_api(pw_page, "GET", f"/cabling/topologies/{topology_id}", allow_errors=True)
            assert gone.status_code == 404, f"topology {topology_id} left behind"
