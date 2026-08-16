"""Playwright e2e test for the multi-port wiring dialog (issue #517).

Draws a connection between two device nodes on the topology canvas (the
product's signature interaction, previously uncovered at any test level per
the design handoff's UI_TEST_PLAN gap 1), wires two port pairs inside the
dialog via the click-to-connect path, sets one line to a non-default layer,
confirms, saves the parent topology, and asserts the effect against the
backend: GET /cabling/topologies/{id} must show exactly two distinct edge
objects with unique ids (the addendum's load-bearing render-only-bundling
invariant: the canvas store, and therefore what gets persisted, never
collapses multiple connections between one device pair into one object) and
the right port names and layers. It also asserts the canvas renders one
bundled edge with a "2 connections" badge for that same pair, and that
Escape closes the dialog without emitting any edge.

Per the #517 implementation addendum's correction to the handoff's original
test plan: the final assertion is the backend read-back above, not a DOM
edge count or the raw PATCH/PUT payload. The transient topology this test
creates is deleted in a finally block, restoring baseline.

Cabling semantics note (review item 1): the two discovered devices' ports may
or may not already carry a registered physical connection to some other
device; either way they are selectable (nothing about cabling state blocks a
port in the dialog), so the test does not need to special-case or filter for
uncabled ports.

Handle-to-handle dragging on the React Flow canvas has no existing Playwright
(or Selenium) precedent anywhere in this suite; test_fork_live_edit.py's
Selenium tests note plain drag-and-drop is unreliable there and avoid it
entirely. This test attempts the drag with Playwright's mouse API (move to
the source node's right handle, mouse down, move in intermediate steps to the
target node's left handle, mouse up), which is the standard approach for
React Flow specifically and not the same mechanism Selenium struggles with.
If this proves flaky in practice, per the design handoff's own guidance the
remedy is a documented skip marker here plus a follow-up issue, not silently
deleting the coverage.

This file is intended to run under `make test-e2e` (or the nightly suite);
it has not been executed against a live stack as part of this change (the
agent that wrote it was instructed not to touch the running demo stack), so
treat every locator and the drag step in particular as unverified until a
real run confirms them.
"""

import time

from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login, pw_two_devices_with_ports


def test_wiring_dialog_two_pairs_persist_as_distinct_edges(pw_page):
    pw_login(pw_page)
    (device_a, ports_a), (device_b, ports_b) = pw_two_devices_with_ports(pw_page, min_ports=2)
    port_a1, port_a2 = ports_a[0], ports_a[1]
    port_b1, port_b2 = ports_b[0], ports_b[1]

    topology_id = None
    try:
        # --- Seed a transient topology with the two devices already placed.
        # A thin {device: {id}} node ref is enough: the editor hydrates the
        # full device from inventory on load (see hydrateCanvasNodes), the
        # same shape test_fork_live_edit.py seeds.
        topo_name = f"e2e-pw-wiring-{int(time.time() * 1000)}"
        topo = pw_api(pw_page, "POST", "/cabling/topologies", json={"name": topo_name}).json()
        topology_id = topo["id"]
        canvas = {
            "nodes": [
                {
                    "id": "n-src",
                    "type": "deviceNode",
                    "position": {"x": 100, "y": 150},
                    "data": {"device": {"id": device_a["id"]}},
                },
                {
                    "id": "n-tgt",
                    "type": "deviceNode",
                    "position": {"x": 500, "y": 150},
                    "data": {"device": {"id": device_b["id"]}},
                },
            ],
            "edges": [],
        }
        pw_api(pw_page, "PUT", f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})

        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology_id}")
        expect(pw_page.locator(".react-flow")).to_be_visible()
        expect(pw_page.locator('[data-id="n-src"]')).to_be_visible()
        expect(pw_page.locator('[data-id="n-tgt"]')).to_be_visible()

        # --- Draw the connection: drag from the source node's right handle
        # to the target node's left handle. This is the interaction that
        # opens the wiring dialog and, per the addendum, opens it by default
        # (the full dialog is the primary post-draw surface, not the quick
        # popover).
        source_handle = pw_page.locator('[data-id="n-src"] .react-flow__handle-right')
        target_handle = pw_page.locator('[data-id="n-tgt"] .react-flow__handle-left')
        source_box = source_handle.bounding_box()
        target_box = target_handle.bounding_box()
        assert source_box and target_box, "device node handles did not render"
        sx = source_box["x"] + source_box["width"] / 2
        sy = source_box["y"] + source_box["height"] / 2
        tx = target_box["x"] + target_box["width"] / 2
        ty = target_box["y"] + target_box["height"] / 2

        pw_page.mouse.move(sx, sy)
        pw_page.mouse.down()
        # Intermediate moves so React Flow registers a real drag rather than
        # a click, matching the standard pattern for its handle interactions.
        pw_page.mouse.move(sx + (tx - sx) / 2, sy + (ty - sy) / 2, steps=5)
        pw_page.mouse.move(tx, ty, steps=5)
        pw_page.mouse.up()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text(
            f"Wire {device_a['name']} to {device_b['name']}"
        )

        # --- Escape closes the dialog without emitting an edge.
        pw_page.keyboard.press("Escape")
        expect(pw_page.locator("dialog[open]")).to_have_count(0)
        expect(pw_page.locator(".react-flow__edge")).to_have_count(0)

        # --- Redraw and wire two pairs via click-to-connect (the
        # accessibility path, exercised here since it does not depend on the
        # drag gesture above having worked).
        pw_page.mouse.move(sx, sy)
        pw_page.mouse.down()
        pw_page.mouse.move(sx + (tx - sx) / 2, sy + (ty - sy) / 2, steps=5)
        pw_page.mouse.move(tx, ty, steps=5)
        pw_page.mouse.up()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text(
            f"Wire {device_a['name']} to {device_b['name']}"
        )

        # Port clicks are scoped to each side's own column (review item 8a):
        # two similarly-named/numbered seeded devices could otherwise put the
        # same port name in both columns, making a dialog-wide get_by_text
        # ambiguous. The wiring dialog's port row reacts to a full
        # mousedown+mouseup gesture, not React Flow's own click semantics, so
        # a plain Playwright .click() (which dispatches both) is exercising
        # the actual component gesture correctly here.
        source_column = dialog.locator('[data-testid="port-column-source"]')
        target_column = dialog.locator('[data-testid="port-column-target"]')
        source_column.get_by_text(port_a1["name"], exact=True).click()
        target_column.get_by_text(port_b1["name"], exact=True).click()
        source_column.get_by_text(port_a2["name"], exact=True).click()
        target_column.get_by_text(port_b2["name"], exact=True).click()

        # Set the second (most recently added) line to L3: read the pill's
        # testid BEFORE clicking it (review item 8b: clicking a pill replaces
        # it in the DOM with the expanded segmented control, so reading the
        # attribute afterward targets a detached or renumbered element).
        pills = dialog.locator("[data-testid^='line-pill-']")
        expect(pills).to_have_count(2)
        second_pill_testid = pills.nth(1).get_attribute("data-testid")
        pills.nth(1).click()
        line_id = second_pill_testid.removeprefix("line-pill-")
        dialog.locator(f"[data-testid='line-layer-{line_id}-L3']").click()

        # Confirm via the review strip, which lists both lines with their
        # port pair and layer pill (a plain UI acknowledgment check; the
        # authoritative assertion is the backend read-back after save below).
        dialog.get_by_role("button", name="Review (2)").click()
        review = dialog.locator("[data-testid='review-strip']")
        expect(review).to_be_visible()
        rows = review.locator("div").filter(has_text=port_a2["name"])
        expect(rows.first).to_be_visible()

        dialog.get_by_role("button", name="Add 2 connections").click()
        expect(pw_page.locator("dialog[open]")).to_have_count(0)

        # --- The canvas renders one bundled edge (two connections between
        # the same device pair) with a count badge, per the ratified
        # render-only-bundling decision.
        expect(pw_page.get_by_role("button", name="2 connections")).to_be_visible()

        # --- Save the parent topology.
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Topology saved")).to_be_visible()

        # --- Effect assertion: read the persisted canvas back from the
        # backend (not a DOM count, not the raw PUT payload) and confirm two
        # DISTINCT edge objects landed, each with its own id, carrying the
        # right ports and layers. This is the addendum's load-bearing check:
        # bundling on the canvas must never have collapsed the stored edges.
        saved = pw_api(pw_page, "GET", f"/cabling/topologies/{topology_id}").json()
        edges = saved["canvas_data"]["edges"]
        assert len(edges) == 2, f"expected 2 distinct persisted edges, got {len(edges)}"
        ids = {e["id"] for e in edges}
        assert len(ids) == 2, "persisted edges must carry unique ids (edge_key), not a shared one"

        by_source_port = {e["data"]["source_port_name"]: e for e in edges}
        first = by_source_port[port_a1["name"]]
        assert first["data"]["target_port_name"] == port_b1["name"]
        second = by_source_port[port_a2["name"]]
        assert second["data"]["target_port_name"] == port_b2["name"]
        assert second["data"]["layer"] == "L3"
    finally:
        if topology_id:
            pw_api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)
