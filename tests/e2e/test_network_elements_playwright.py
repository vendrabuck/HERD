"""Playwright e2e coverage for network element objects (ADR 0012 phase 3, issue #22).

Covers the v1 acceptance criteria this ADR's "Testing" section assigns to the
e2e level: drop a network element from the Equipment Browser onto the canvas,
attach two device ports to it through `ElementAttachDialog`, save, reload, and
prove via API READ-BACK (the standing e2e effect-assertion rule; see
CLAUDE.md's E2E section) that `canvas_data` holds the element node and both
attachment edges with `source_port_name` set; that `POST
/cabling/topologies/{id}/validate` reports `valid: true`; and that a
reservation created against the topology commits (ACTIVE via read-back), the
fork's canvas carries the element node, and neither the fork save response
nor the wiring-status read-back shows any element-endpoint wiring.

UI-driven versus API-driven, precisely:

- UI-driven: the Equipment Browser drag-drop of a network element card onto
  the canvas (native HTML5 DragEvent, since a network element card is a
  `draggable` div using `dataTransfer.setData`/`effectAllowed`, not a React
  Flow node, so React Flow's own drag machinery is not involved and a plain
  DragEvent dispatch is reliable, unlike node-internal handle dragging); the
  device-to-element attachment gesture (a genuine mouse
  move/down/move/move/up sequence from the device node's bottom handle to
  the element node's top handle, the same technique
  test_wiring_dialog_playwright.py already uses for device-to-device
  drawing, since NetworkElementNode.tsx exposes exactly one `target` handle
  at Position.Top and DeviceNode.tsx exposes four `source` handles, so a
  device-sourced drag onto that single handle is well defined); the
  ElementAttachDialog port selection (PortColumn's own click-to-toggle
  affordance, the same interaction pattern the wiring dialog's port columns
  use) and its Attach confirm; the Save button and the reload.
- API-driven: topology creation (name only; the drop targets an
  already-created empty topology so screenToFlowPosition has a canvas to
  drop onto), the reservation and its ACTIVE poll, the validate call, the
  fork read-back, the fork save call (a direct POST mirroring exactly what
  the UI's own Save button issues, since ADR 0012 "Fork save and execution"
  is a backend response-shape addition with no new UI affordance beyond the
  toast ForkSaveResultToast.tsx already renders), the wiring-status
  read-back, and all cleanup deletes.

Per the ADR's phase 3 seeding-trap note (issue #629, PR #631 open, not yet
merged as of this test's authorship): both `make everything` and
`nightly.yml` run e2e BEFORE `make seed`, so a device-gated test like this one
skips silently in every automated run today. Each test below self-provisions
and skips with a clear reason string when no AVAILABLE DUT device exists, and
must be run explicitly against a seeded stack; a green automated run is not
evidence these executed until #629/PR #631 lands.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

ACTIVE_POLL_SECONDS = 30


def _find_available_dut(page):
    """One seeded DUT device with at least 2 ports, status AVAILABLE and exclusive.

    Mirrors pw_two_devices_with_ports' AVAILABLE/exclusive filter (conftest.py)
    and _pw_create_reserved_topology's dut_only filter (test_fork_live_edit.py),
    but needs only one device with 2+ ports (both attachment edges land on the
    SAME device, since a single-device topology is sufficient to exercise the
    element/attachment behavior this ADR describes; nothing here depends on a
    second device).
    """
    resp = pw_api(page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True)
    if resp.status_code != 200:
        return None
    payload = resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    for d in items:
        if d.get("status") != "AVAILABLE" or not d.get("exclusive", True):
            continue
        ports = pw_api(page, "GET", f"/inventory/devices/{d['id']}/ports").json()
        if len(ports) >= 2:
            return d, ports
    return None


def _create_empty_topology(page, name):
    resp = pw_api(page, "POST", "/cabling/topologies", json={"name": name}, allow_errors=True)
    if resp.status_code not in (200, 201):
        return None
    return resp.json()


def _drop_network_element(page, element_type: str, label: str, x: int, y: int):
    """Drop a network element card from the Equipment Browser onto the canvas.

    NetworkElementCard (EquipmentBrowser.tsx) is a plain `draggable` div that
    calls `dataTransfer.setData("application/herd-network-element", ...)` on
    dragstart; it is not a React Flow node, so this does not need React
    Flow's handle-drag machinery (unlike attaching a port below). A native
    HTML5 drag-and-drop sequence needs a real DataTransfer carried across
    dragstart/dragover/drop, which Playwright's high-level API does not
    synthesize on its own, so this dispatches the three events directly via
    page.evaluate against the actual DOM elements.
    """
    card = page.locator(f"text='{label}'").locator("xpath=ancestor::div[@draggable='true']")
    expect(card).to_be_visible(timeout=10_000)
    canvas = page.locator(".react-flow__pane")
    expect(canvas).to_be_visible()

    page.evaluate(
        """([cardSel, x, y, mime, payload]) => {
            const card = document.evaluate(cardSel, document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            const pane = document.querySelector('.react-flow__pane');
            const dt = new DataTransfer();
            dt.setData(mime, payload);

            const dragStart = new DragEvent('dragstart', {
                bubbles: true, cancelable: true, dataTransfer: dt,
            });
            card.dispatchEvent(dragStart);

            const rect = pane.getBoundingClientRect();
            const clientX = rect.left + x;
            const clientY = rect.top + y;

            const dragOver = new DragEvent('dragover', {
                bubbles: true, cancelable: true, dataTransfer: dt,
                clientX, clientY,
            });
            pane.dispatchEvent(dragOver);

            const drop = new DragEvent('drop', {
                bubbles: true, cancelable: true, dataTransfer: dt,
                clientX, clientY,
            });
            pane.dispatchEvent(drop);
        }""",
        [
            f"//*[text()='{label}']/ancestor::div[@draggable='true']",
            x,
            y,
            "application/herd-network-element",
            f'{{"element_type": "{element_type}", "label": "{label}"}}',
        ],
    )


def test_drop_attach_save_reload_validate(pw_page):
    """Drop a vlan_segment element, attach two device ports, save, reload, validate.

    Asserts via API read-back that canvas_data holds the element node and
    both attachment edges with source_port_name set, and that the validate
    endpoint reports valid: true for the resulting canvas.
    """
    pw_login(pw_page)
    found = _find_available_dut(pw_page)
    if not found:
        pytest.skip("no AVAILABLE, exclusive DUT device with at least 2 ports (issue #629)")
    device, ports = found
    port_a, port_b = ports[0], ports[1]

    topo_name = f"e2e-elements-{uuid.uuid4().hex[:8]}"
    topology_id = None
    try:
        topology = _create_empty_topology(pw_page, topo_name)
        assert topology, "topology creation failed"
        topology_id = topology["id"]

        canvas = {
            "nodes": [
                {
                    "id": "n-dev",
                    "type": "deviceNode",
                    "position": {"x": 150, "y": 200},
                    "data": {"device": {"id": device["id"]}},
                }
            ],
            "edges": [],
        }
        pw_api(pw_page, "PUT", f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})

        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology_id}")
        expect(pw_page.locator(".react-flow")).to_be_visible(timeout=15_000)
        expect(pw_page.locator('[data-id="n-dev"]')).to_be_visible()

        # --- UI-driven: drag the vlan_segment card from the Equipment
        # Browser's "Network elements" section onto the canvas, away from the
        # device node so the two are not overlapping. The section is
        # unconditional and expanded by default (showNetworkElements starts
        # true, unlike the dynamic-templates section), so no toggle click is
        # needed; clicking the header here would instead COLLAPSE it.
        elements_toggle = pw_page.get_by_role("button", name="Network elements")
        expect(elements_toggle).to_be_visible(timeout=10_000)
        expect(elements_toggle).to_have_attribute("aria-expanded", "true")
        _drop_network_element(pw_page, "vlan_segment", "VLAN segment", x=500, y=200)

        element_node = pw_page.locator(".react-flow__node").filter(has_text="VLAN segment")
        expect(element_node).to_have_count(1, timeout=10_000)
        element_node_id = element_node.get_attribute("data-id")
        assert element_node_id, "network element node did not render with a data-id"

        # --- UI-driven: drag from the device node's bottom handle to the
        # element node's top handle (its only handle) to open
        # ElementAttachDialog. Same mouse move/down/move/move/up technique as
        # test_wiring_dialog_playwright.py's device-to-device draw.
        source_handle = pw_page.locator('[data-id="n-dev"] .react-flow__handle-bottom')
        target_handle = pw_page.locator(f'[data-id="{element_node_id}"] .react-flow__handle-top')
        source_box = source_handle.bounding_box()
        target_box = target_handle.bounding_box()
        assert source_box and target_box, "device or element node handle did not render"
        sx = source_box["x"] + source_box["width"] / 2
        sy = source_box["y"] + source_box["height"] / 2
        tx = target_box["x"] + target_box["width"] / 2
        ty = target_box["y"] + target_box["height"] / 2

        pw_page.mouse.move(sx, sy)
        pw_page.mouse.down()
        pw_page.mouse.move(sx + (tx - sx) / 2, sy + (ty - sy) / 2, steps=5)
        pw_page.mouse.move(tx, ty, steps=5)
        pw_page.mouse.up()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text(
            f"Attach {device['name']} to VLAN segment", timeout=10_000
        )

        # --- UI-driven: select two ports in the single PortColumn (multi-
        # select toggle, unlike WiringDialog's arm-then-pair model) and
        # confirm.
        source_column = dialog.locator('[data-testid="port-column-source"]')
        source_column.get_by_text(port_a["name"], exact=True).click()
        source_column.get_by_text(port_b["name"], exact=True).click()
        dialog.get_by_role("button", name="Attach 2 ports").click()
        expect(pw_page.locator("dialog[open]")).to_have_count(0)

        # --- The canvas renders one bundled edge (two attachments to the
        # same element) with a count badge, per the render-only-bundling
        # decision this ADR reuses unchanged.
        expect(pw_page.get_by_role("button", name="2 connections")).to_be_visible()

        # --- Save, then reload the page fresh (a real navigation, not just
        # re-fetch) to prove the element and its edges round-trip through a
        # full load, not just an in-memory store.
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Topology saved")).to_be_visible(timeout=10_000)

        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology_id}")
        expect(pw_page.locator(".react-flow")).to_be_visible(timeout=15_000)
        expect(pw_page.locator(".react-flow__node").filter(has_text="VLAN segment")).to_have_count(
            1, timeout=10_000
        )

        # --- API-driven effect assertion: read the persisted canvas back
        # from the backend and confirm the element node and both attachment
        # edges landed, each with source_port_name set and the element as
        # the normalized target.
        saved = pw_api(pw_page, "GET", f"/cabling/topologies/{topology_id}").json()
        nodes = saved["canvas_data"]["nodes"]
        edges = saved["canvas_data"]["edges"]

        element_nodes = [n for n in nodes if n["type"] == "networkElementNode"]
        assert len(element_nodes) == 1, (
            f"expected 1 persisted element node, got {len(element_nodes)}"
        )
        assert element_nodes[0]["data"]["element"]["element_type"] == "vlan_segment"
        persisted_element_node_id = element_nodes[0]["id"]

        attachment_edges = [e for e in edges if e["target"] == persisted_element_node_id]
        assert len(attachment_edges) == 2, (
            f"expected 2 persisted attachment edges, got {len(attachment_edges)}"
        )
        ids = {e["id"] for e in attachment_edges}
        assert len(ids) == 2, "attachment edges must carry unique ids"
        assert all(e["source"] == "n-dev" for e in attachment_edges), (
            "device must always be normalized as the edge source"
        )
        source_port_names = {e["data"]["source_port_name"] for e in attachment_edges}
        assert source_port_names == {port_a["name"], port_b["name"]}

        # --- API-driven: the validate endpoint reports valid: true for this
        # canvas (an attachment edge classifies as VALID with no BFS).
        validation = pw_api(pw_page, "POST", f"/cabling/topologies/{topology_id}/validate").json()
        assert validation["valid"] is True, f"expected valid topology, got {validation}"
        assert validation["invalid_edges"] == []
    finally:
        if topology_id:
            pw_api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)


def test_reservation_commits_and_fork_carries_element(pw_page):
    """A reservation created against an element-carrying topology activates,
    and its fork's canvas carries the element node with zero element-endpoint
    wiring rows, per the ADR's fork-save invariant that an element edge never
    becomes a hop.

    The element and its attachment are seeded via the API (a thin canvas PUT,
    the same seeding technique test_fork_live_edit.py and
    test_wiring_dialog_playwright.py both use for their device nodes) rather
    than re-driving the UI drag/attach flow already proven live above, since
    this test's own subject is the reservation/fork backend path, not the
    canvas interaction.
    """
    pw_login(pw_page)
    found = _find_available_dut(pw_page)
    if not found:
        pytest.skip("no AVAILABLE, exclusive DUT device with at least 2 ports (issue #629)")
    device, ports = found
    port_a = ports[0]

    topo_name = f"e2e-elements-res-{uuid.uuid4().hex[:8]}"
    topology_id = None
    reservation_id = None
    try:
        topology = _create_empty_topology(pw_page, topo_name)
        assert topology, "topology creation failed"
        topology_id = topology["id"]

        element_node_id = "n-elem"
        canvas = {
            "nodes": [
                {
                    "id": "n-dev",
                    "type": "deviceNode",
                    "position": {"x": 100, "y": 100},
                    "data": {"device": {"id": device["id"]}},
                },
                {
                    "id": element_node_id,
                    "type": "networkElementNode",
                    "position": {"x": 400, "y": 100},
                    "data": {
                        "element": {
                            "id": str(uuid.uuid4()),
                            "element_type": "vlan_segment",
                            "label": "res-vlan",
                            "attrs": {},
                        }
                    },
                },
            ],
            "edges": [
                {
                    "id": "e-attach",
                    "source": "n-dev",
                    "target": element_node_id,
                    "type": "layerEdge",
                    "data": {
                        "layer": "L1",
                        "source_port_id": port_a["id"],
                        "source_port_name": port_a["name"],
                    },
                }
            ],
        }
        canvas_resp = pw_api(
            pw_page,
            "PUT",
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": canvas},
            allow_errors=True,
        )
        assert canvas_resp.status_code == 200, canvas_resp.text

        # --- API-driven: confirm the seeded canvas validates before booking,
        # same as the UI's Reserve Topology gate would check.
        validation = pw_api(pw_page, "POST", f"/cabling/topologies/{topology_id}/validate").json()
        assert validation["valid"] is True, f"seeded canvas should validate, got {validation}"

        now = datetime.now(timezone.utc)
        res_resp = pw_api(
            pw_page,
            "POST",
            "/reservations/",
            json={
                "device_ids": [device["id"]],
                "topology_id": topology_id,
                "purpose": "e2e network element commit",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(minutes=30)).isoformat(),
            },
            allow_errors=True,
        )
        assert res_resp.status_code == 201, res_resp.text
        reservation = res_resp.json()
        reservation_id = reservation["id"]

        # --- API-driven: poll to ACTIVE (the reservations gate at
        # reservation_service.py:1139 accepting an element-carrying canvas is
        # the acceptance criterion issue #22 names).
        deadline = time.time() + ACTIVE_POLL_SECONDS
        status = reservation.get("status")
        while status != "ACTIVE" and time.time() < deadline:
            time.sleep(1)
            poll = pw_api(pw_page, "GET", f"/reservations/{reservation_id}", allow_errors=True)
            if poll.status_code == 200:
                status = poll.json().get("status")
        assert status == "ACTIVE", f"reservation never reached ACTIVE (last status: {status})"

        # --- API-driven effect assertion: the fork's canvas carries the
        # element node.
        fork = pw_api(pw_page, "GET", f"/reservations/{reservation_id}/fork").json()
        fork_canvas = fork["canvas_data"]
        fork_element_nodes = [n for n in fork_canvas["nodes"] if n["type"] == "networkElementNode"]
        assert len(fork_element_nodes) == 1, (
            f"expected the fork snapshot to carry the element node, got {fork_canvas['nodes']}"
        )
        assert fork_element_nodes[0]["data"]["element"]["element_type"] == "vlan_segment"

        # --- API-driven: a fork save reconcile (the same call the UI's Save
        # button issues while editing a live reservation, per
        # ForkSaveResultToast.tsx) reports element_attachments_skipped and
        # creates zero fork_connections rows carrying the element endpoint
        # (ADR 0012 "Fork save and execution": an element edge never becomes
        # a hop).
        save_resp = pw_api(
            pw_page,
            "POST",
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": fork_canvas},
            allow_errors=True,
        )
        assert save_resp.status_code == 200, save_resp.text
        save_result = save_resp.json()
        assert save_result["element_attachments_skipped"] == 1, save_result
        assert save_result["released"] == []
        assert save_result["built"] == []

        # --- API-driven: the wiring-status read-back shows zero
        # element-endpoint wiring rows (here, zero rows at all, since the
        # topology's only edge is the element attachment and no
        # device-to-device wire exists to produce a hop).
        wiring = pw_api(pw_page, "GET", f"/reservations/{reservation_id}/wiring-status").json()
        assert wiring["connections"] == [], wiring
    finally:
        if reservation_id:
            pw_api(pw_page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)
        if topology_id:
            pw_api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)
