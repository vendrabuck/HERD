"""Playwright e2e for the topology editor's canvas identity (issue #1315).

The canvas store is global in the browser, so before the fix opening topology
B after topology A showed A's canvas under B's name until B's device hydration
(`POST /api/inventory/devices/batch`) returned, with Save enabled; a Save there
wrote A's wiring into B. This test holds that hydration call with `page.route`,
opens A, moves to B inside the single-page app, and asserts Save is disabled and
A's node is absent until the held call is released; then it saves B and reads
both topologies back through the API.

The move from A to B is an in-app navigation (`history.pushState` plus a
`popstate` event, which React Router's BrowserRouter handles without a page
load), the same in-place id change the AI commit dialog makes; a `page.goto`
would reload the app and start from an empty store, hiding the bug.

Self-seeding: two topologies under a per-run prefix, each with one device node
(two seeded devices from `pw_two_devices_with_ports`). No cable, reservation, or
port is created, so teardown deletes the two topologies and checks both are gone.
Device-gated: on the unseeded first e2e pass there is no device and the test
skips; the seeded pass must not.
"""

import re
import uuid

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect

from .conftest import (
    HOST_BASE_URL,
    log_cleanup_failure,
    pw_api,
    pw_login,
    pw_two_devices_with_ports,
)

WAIT_MS = 15_000
BATCH_ROUTE = re.compile(r".*/api/inventory/devices/batch$")


def _canvas(node_id: str, device_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": node_id,
                "type": "deviceNode",
                "position": {"x": 200, "y": 200},
                "data": {"device": {"id": device_id}},
            }
        ],
        "edges": [],
        "selectedEdgeLayer": "L2",
    }


def _node_ids(page, topology_id: str) -> list[str]:
    topo = pw_api(page, "GET", f"/cabling/topologies/{topology_id}").json()
    return [n["id"] for n in (topo.get("canvas_data") or {}).get("nodes", [])]


def _navigate_in_app(page, path: str) -> None:
    page.evaluate(
        "(path) => { window.history.pushState({}, '', path);"
        " window.dispatchEvent(new PopStateEvent('popstate')); }",
        path,
    )


def test_opening_b_after_a_never_saves_a_canvas_into_b(pw_page):
    pw_login(pw_page)
    (device_a, _), (device_b, _) = pw_two_devices_with_ports(pw_page)
    prefix = f"pw1315-{uuid.uuid4().hex[:8]}"
    created: list[str] = []
    held: list = []
    try:
        for suffix, node_id, device in (("a", "n-a", device_a), ("b", "n-b", device_b)):
            topo = pw_api(
                pw_page, "POST", "/cabling/topologies", json={"name": f"{prefix}-{suffix}"}
            ).json()
            created.append(topo["id"])
            pw_api(
                pw_page,
                "PUT",
                f"/cabling/topologies/{topo['id']}",
                json={"canvas_data": _canvas(node_id, device["id"])},
            )
        topo_a, topo_b = created

        pw_page.goto(f"{HOST_BASE_URL}/topology/{topo_a}")
        expect(pw_page.locator('[data-id="n-a"]')).to_be_visible(timeout=WAIT_MS)
        save = pw_page.get_by_role("button", name="Save", exact=True)
        expect(save).to_be_enabled(timeout=WAIT_MS)

        # The handler holds B's device batch call until the test releases it.
        # Held routes are continued while the handler is still installed:
        # removing the handler first leaves them already handled.
        releasing = {"now": False}

        def _hold_or_pass(route):
            if releasing["now"]:
                route.continue_()
            else:
                held.append(route)

        pw_page.route(BATCH_ROUTE, _hold_or_pass)
        with pw_page.expect_request(BATCH_ROUTE, timeout=WAIT_MS):
            _navigate_in_app(pw_page, f"/topology/{topo_b}")
        expect(pw_page.get_by_text(f"{prefix}-b", exact=True)).to_be_visible(timeout=WAIT_MS)
        expect(pw_page.get_by_role("status").filter(has_text="Loading canvas...")).to_be_visible()
        assert held, "B's device batch call was not held"
        expect(save).to_be_disabled()
        expect(pw_page.locator('[data-id="n-a"]')).to_have_count(0)

        releasing["now"] = True
        while held:
            held.pop().continue_()
        expect(pw_page.locator('[data-id="n-b"]')).to_be_visible(timeout=WAIT_MS)
        expect(pw_page.locator('[data-id="n-a"]')).to_have_count(0)
        expect(save).to_be_enabled(timeout=WAIT_MS)

        with pw_page.expect_response(
            lambda r: (
                r.request.method == "PUT" and r.url.endswith(f"/api/cabling/topologies/{topo_b}")
            )
        ) as put:
            save.click()
        assert put.value.status == 200, put.value.status

        assert _node_ids(pw_page, topo_b) == ["n-b"]
        assert _node_ids(pw_page, topo_a) == ["n-a"]
    finally:
        while held:
            try:
                held.pop().continue_()
            except PlaywrightError:
                # The page or the route is already gone; cleanup goes on.
                pass
        for topology_id in created:
            resp = pw_api(
                pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True
            )
            if resp.status_code != 404:
                log_cleanup_failure("topology", topology_id, resp)
            gone = pw_api(pw_page, "GET", f"/cabling/topologies/{topology_id}", allow_errors=True)
            assert gone.status_code == 404, f"topology {topology_id} left behind"
