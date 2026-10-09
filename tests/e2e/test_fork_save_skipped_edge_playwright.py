"""Playwright e2e: the fork save names a line it could not wire (issue #1007).

A canvas line that names its ports (`source_port_name`, `target_port_name`) is
wired only on those ports; the fork save never falls back to another port pair
(issue #531). When no cable path joins the chosen ports, the save still answers
200, builds nothing for that line, and since issue #1007 lists it in
`constrained_edges_skipped`; the editor's save toast names it.

The editor's own pre-save signals judge a line by its device pair and by
whether each chosen port has some cable, so a line whose two ports exist but
are not joined to each other passes them. The save toast is what tells the
user. This test drives exactly that case through the live editor:

- API-driven setup: a driver, two templates, two FRESH devices with three
  ports each (issue #670: fresh devices carry no cabling, independent of the
  shared stack's history), one cable joining port 1 of each device, a topology
  holding both devices and the cabled line, and a reservation booked on it
  (polled to ACTIVE). The draft fork canvas is then written with a second line
  on port 2 of each device, which no cable joins.
- UI-driven (issue #1066): open the live editor and move a node, which sends a
  draft autosave PUT; its `invalid_edges` names the unjoined line, so the line
  turns red before any save: the bundle's member list shows "no cable path on
  the chosen ports" with the reason on hover, and the live-edit bar counts the
  line without blocking Commit.
- UI-driven: click "Commit to reservation", and assert the toast names the
  dropped line by device name and port, and the line is still marked.
- API read-back: the fork's saved connections hold only the cabled pair, and
  the user-facing validate route reports the second line as `no_port_path`
  (the pre-check that agrees with the save, issue #1047).

Cleanup cancels the reservation before deleting the topology, deletes the
cable before the devices (the teardown contract), and runs from a finally
block so a failure part way through leaves nothing behind.

Not run when written: it needs a running stack; nightly and the gates run it.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, driver_tarball, log_cleanup_failure, pw_api, pw_login

ACTIVE_POLL_SECONDS = 30
WAIT_MS = 15_000
NOT_WIRED_HEADING = "1 line not wired: no cable path on the chosen ports"
NO_PORT_PATH_TEXT = "no cable path on the chosen ports"
DRAFT_CHECK_NOTE = (
    "1 line failed the last draft check; committing does not wire it. "
    "Hover its label for the reason."
)


def _park_pointer(page) -> None:
    # react-hot-toast pauses a toast while the pointer is over the toaster
    # (bottom centre since issue #942); the left edge is clear of it.
    page.mouse.move(5, 400)


def _move_node(page, node_id: str) -> None:
    # Dragging a node changes its position, which the fork autosave treats as
    # an edit: a draft PUT follows after the debounce.
    box = page.locator(f'[data-id="{node_id}"]').bounding_box()
    assert box is not None, f"node {node_id} has no bounding box"
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 40, y + 60, steps=8)
    page.mouse.up()


def _poll_active(page, reservation_id: str) -> str:
    deadline = time.time() + ACTIVE_POLL_SECONDS
    status = None
    while time.time() < deadline:
        poll = pw_api(page, "GET", f"/reservations/{reservation_id}", allow_errors=True)
        if poll.status_code == 200:
            status = poll.json().get("status")
            if status in ("ACTIVE", "FAILED", "CANCELLED", "COMPLETED"):
                return status
        time.sleep(1)
    return status or "unknown"


def _device_node(node_id: str, device_id: str, x: int) -> dict:
    return {
        "id": node_id,
        "type": "deviceNode",
        "position": {"x": x, "y": 150},
        "data": {"device": {"id": device_id}},
    }


def _line(edge_id: str, source_port: str, target_port: str) -> dict:
    return {
        "id": edge_id,
        "source": "n-a",
        "target": "n-b",
        "type": "layerEdge",
        "data": {
            "layer": "L1",
            "source_port_name": source_port,
            "target_port_name": target_port,
        },
    }


def test_fork_save_toast_names_a_line_on_unjoined_ports(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:10]
    driver_id = device_template_id = port_template_id = None
    device_ids: list[str] = []
    cable_id = topology_id = reservation_id = None

    try:
        # --- API-driven setup: driver, templates, two fresh devices.
        files = {"file": ("e2e-skip-driver.tar.gz", driver_tarball(), "application/gzip")}
        driver_id = pw_api(
            pw_page,
            "POST",
            "/inventory/drivers",
            files=files,
            data={
                "name": f"e2e-pw-skip-drv-{suffix}",
                "connection_type": "Management",
                "description": "fork save skipped edge e2e driver",
            },
        ).json()["id"]
        device_template_id = pw_api(
            pw_page,
            "POST",
            "/inventory/templates",
            json={
                "name": f"e2e-pw-skip-devtmpl-{suffix}",
                "template_type": "device",
                "driver_id": driver_id,
                "vendor": "e2e-pw-skip",
                "model": "fixture",
                "sections": [{"name": "General", "fields": []}],
            },
        ).json()["id"]
        port_template_id = pw_api(
            pw_page,
            "POST",
            "/inventory/templates",
            json={
                "name": f"e2e-pw-skip-porttmpl-{suffix}",
                "template_type": "port",
                "sections": [{"name": "General", "fields": []}],
            },
        ).json()["id"]

        devices = []
        for label in ("a", "b"):
            device = pw_api(
                pw_page,
                "POST",
                "/inventory/devices",
                json={
                    "name": f"e2e-pw-skip-dev-{label}-{suffix}",
                    "template_id": device_template_id,
                    "topology_type": "PHYSICAL",
                },
            ).json()
            device_ids.append(device["id"])
            ports = pw_api(
                pw_page,
                "POST",
                f"/inventory/devices/{device['id']}/ports/bulk",
                json={
                    "name_prefix": f"e2e-{label}-",
                    "starting_index": 1,
                    "instances": 3,
                    "template_id": port_template_id,
                },
            ).json()
            names = sorted(p["name"] for p in ports)
            assert len(names) == 3, f"expected 3 ports on device {label}, got {names}"
            devices.append((device, names))
        (device_a, ports_a), (device_b, ports_b) = devices

        # One cable joins port 1 of each device; port 2 of each stays unjoined.
        cable_id = pw_api(
            pw_page,
            "POST",
            "/cabling/connections",
            json={
                "device_a_id": device_a["id"],
                "port_a": ports_a[0],
                "device_b_id": device_b["id"],
                "port_b": ports_b[0],
                "notes": f"e2e-pw-skip-{suffix}",
            },
        ).json()["id"]

        # --- API-driven: a valid topology (the cabled line only) and a booking.
        nodes = [
            _device_node("n-a", device_a["id"], 100),
            _device_node("n-b", device_b["id"], 450),
        ]
        cabled_line = _line("e-cabled", ports_a[0], ports_b[0])
        topology_id = pw_api(
            pw_page, "POST", "/cabling/topologies", json={"name": f"e2e-pw-skip-{suffix}"}
        ).json()["id"]
        pw_api(
            pw_page,
            "PUT",
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": {"nodes": nodes, "edges": [cabled_line]}},
        )
        now = datetime.now(timezone.utc)
        res_resp = pw_api(
            pw_page,
            "POST",
            "/reservations/",
            json={
                "device_ids": [device_a["id"], device_b["id"]],
                "topology_id": topology_id,
                "purpose": f"e2e fork save skipped edge {suffix}",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(minutes=30)).isoformat(),
            },
            allow_errors=True,
        )
        assert res_resp.status_code == 201, res_resp.text
        reservation_id = res_resp.json()["id"]
        status = _poll_active(pw_page, reservation_id)
        assert status == "ACTIVE", f"reservation never reached ACTIVE (last status: {status})"

        # --- API-driven: the draft gains a second line on ports no cable joins.
        unjoined_line = _line("e-unjoined", ports_a[1], ports_b[1])
        draft = pw_api(
            pw_page,
            "PUT",
            f"/reservations/{reservation_id}/fork/canvas",
            json={"canvas_data": {"nodes": nodes, "edges": [cabled_line, unjoined_line]}},
        ).json()
        # The loose draft write stores regardless but already reports the line.
        assert [(e["edge_id"], e["reason"]) for e in draft["invalid_edges"]] == [
            ("e-unjoined", "no_port_path")
        ], draft

        # --- UI-driven: commit from the live editor.
        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology_id}?reservationId={reservation_id}")
        expect(pw_page.locator(".react-flow")).to_be_visible(timeout=WAIT_MS)
        expect(pw_page.locator('[data-id="n-a"]')).to_be_visible(timeout=WAIT_MS)
        commit = pw_page.get_by_role("button", name="Commit to reservation")
        expect(commit).to_be_enabled(timeout=WAIT_MS)

        # --- UI-driven (issue #1066): an edit sends a draft PUT whose answer
        # marks the unjoined line red before the save.
        with pw_page.expect_response(
            lambda r: (
                r.url.endswith(f"/reservations/{reservation_id}/fork/canvas")
                and r.request.method == "PUT"
            ),
            timeout=WAIT_MS,
        ) as put_info:
            _move_node(pw_page, "n-b")
        put_body = put_info.value.json()
        assert [(e["edge_id"], e["reason"]) for e in put_body["invalid_edges"]] == [
            ("e-unjoined", "no_port_path")
        ], put_body
        # Both lines share the device pair, so they render as one bundle.
        bundle = pw_page.get_by_role("button", name="2 connections")
        expect(bundle).to_be_visible(timeout=WAIT_MS)
        expect(bundle).to_have_css("color", "rgb(239, 68, 68)", timeout=WAIT_MS)
        bundle.click()
        reason = pw_page.get_by_text(NO_PORT_PATH_TEXT, exact=True)
        expect(reason).to_be_visible(timeout=WAIT_MS)
        expect(reason).to_have_attribute(
            "title",
            f"The last draft check reported this line: {NO_PORT_PATH_TEXT}. "
            "Committing does not wire it.",
        )
        expect(pw_page.get_by_text(DRAFT_CHECK_NOTE, exact=True)).to_be_visible()
        # Not a commit block: the save still wires the cabled line.
        expect(commit).to_be_enabled()

        with pw_page.expect_response(
            lambda r: (
                r.url.endswith(f"/reservations/{reservation_id}/fork/save")
                and r.request.method == "POST"
            )
        ) as save_info:
            commit.click()
        _park_pointer(pw_page)
        save_response = save_info.value
        assert save_response.status == 200, save_response.text()
        body = save_response.json()
        assert body["constrained_edges_skipped"] == [
            {
                "edge_id": "e-unjoined",
                "source_device_id": device_a["id"],
                "target_device_id": device_b["id"],
                "source_port_name": ports_a[1],
                "target_port_name": ports_b[1],
            }
        ], body

        # The toast names the dropped line by device name and port.
        alert = pw_page.get_by_role("alert").filter(has_text=NOT_WIRED_HEADING)
        expect(alert).to_be_visible(timeout=WAIT_MS)
        expect(
            alert.get_by_text(
                f"{device_a['name']} {ports_a[1]} to {device_b['name']} {ports_b[1]}",
                exact=True,
            )
        ).to_be_visible()

        # --- API read-back: only the cabled pair is in the saved wiring.
        fork = pw_api(pw_page, "GET", f"/reservations/{reservation_id}/fork").json()
        wired = {
            frozenset({(c["device_a_id"], c["port_a"]), (c["device_b_id"], c["port_b"])})
            for c in fork.get("connections", [])
        }
        assert wired == {frozenset({(device_a["id"], ports_a[0]), (device_b["id"], ports_b[0])})}, (
            fork.get("connections")
        )

        # The toast stays until dismissed when it carries a not-wired line.
        pw_page.get_by_role("button", name="Dismiss").first.click()
        expect(pw_page.get_by_text(NOT_WIRED_HEADING, exact=True)).to_have_count(0)

        # The save skipped the line, so it stays marked after the commit.
        expect(pw_page.get_by_text(NO_PORT_PATH_TEXT, exact=True)).to_be_visible()
        expect(pw_page.get_by_text(DRAFT_CHECK_NOTE, exact=True)).to_be_visible()
    finally:
        if reservation_id:
            # DELETE cancels; the row stays in the list as CANCELLED.
            resp = pw_api(pw_page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)
            log_cleanup_failure("reservation", reservation_id, resp)
        if topology_id:
            resp = pw_api(
                pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True
            )
            log_cleanup_failure("topology", topology_id, resp)
        if cable_id:
            resp = pw_api(pw_page, "DELETE", f"/cabling/connections/{cable_id}", allow_errors=True)
            log_cleanup_failure("connection", cable_id, resp)
        for device_id in device_ids:
            resp = pw_api(pw_page, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True)
            log_cleanup_failure("device", device_id, resp)
        for template_id in (device_template_id, port_template_id):
            if template_id:
                resp = pw_api(
                    pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True
                )
                log_cleanup_failure("template", template_id, resp)
        if driver_id:
            resp = pw_api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
            log_cleanup_failure("driver", driver_id, resp)
