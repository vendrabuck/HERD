"""Playwright e2e test for connection create/delete with effect assertions
(issue #388 item 3).

Creates a backend connection end to end through the admin Connections UI
(frontend/src/pages/admin/ConnectionsPage.tsx: device search, port selects,
Create Connection modal), verifies it via the cabling service API, deletes it
through the same page's row action, and confirms it is gone via the API.

Two seeded devices with at least one port each are discovered dynamically
through the inventory API (host-side, not the UI) rather than hardcoded, so
the test does not depend on exact seed data/order.
"""

import time

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login


def _token(page) -> str | None:
    return page.evaluate("() => window.localStorage.getItem('access_token')")


def _api(page, method, path, **kwargs):
    """Authenticated host-side HERD API request, using the browser's own JWT."""
    allow_errors = kwargs.pop("allow_errors", False)
    token = _token(page)
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{HOST_BASE_URL}/api{path}"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.request(method, url, headers=headers, **kwargs)
    if not allow_errors:
        resp.raise_for_status()
    return resp


def _two_devices_with_ports(page):
    """Find two distinct seeded devices that each have at least one port.

    Scans devices via the inventory API (same data the ConnectionsPage device
    search hits) until two qualifying devices are found, capped so a slow or
    empty inventory skips rather than hangs. Must be called after login: it
    needs the browser's JWT to call the API.
    """
    devices = _api(page, "GET", "/inventory/devices", params={"skip": 0, "limit": 100}).json()[
        "items"
    ]
    found = []
    for d in devices:
        ports = _api(page, "GET", f"/inventory/devices/{d['id']}/ports").json()
        if ports:
            found.append((d, ports[0]))
        if len(found) == 2:
            break
    if len(found) < 2:
        pytest.skip("fewer than two seeded devices with ports available")
    return found[0], found[1]


def _select_device(page, device_name: str) -> None:
    """Type into the next unresolved device search box and pick the match.

    Both Device A and Device B render a "Search devices..." input until
    picked, at which point that input unmounts (replaced by a name + Change
    button). `.first` therefore always targets Device A on the first call and
    Device B on the second, without tracking indices across the mutation.
    """
    search_input = page.get_by_placeholder("Search devices...").first
    search_input.fill(device_name)
    page.get_by_role("button", name=device_name, exact=True).click()


def test_connection_create_and_delete(pw_page):
    pw_login(pw_page)
    (device_a, port_a), (device_b, port_b) = _two_devices_with_ports(pw_page)
    connection_id = None
    notes = f"e2e-pw-connection-{int(time.time() * 1000)}"

    try:
        pw_page.goto(f"{HOST_BASE_URL}/admin/connections")
        expect(pw_page.get_by_role("button", name="Create Connection")).to_be_visible()
        pw_page.get_by_role("button", name="Create Connection").click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Create Connection")

        # Scoped to the dialog: the connections table underneath can already
        # list many existing rows for these device names (e.g. a hub cabled
        # to many things), which would make an unscoped get_by_text ambiguous.
        _select_device(pw_page, device_a["name"])
        expect(dialog.get_by_text(device_a["name"], exact=True)).to_be_visible()

        _select_device(pw_page, device_b["name"])
        expect(dialog.get_by_text(device_b["name"], exact=True)).to_be_visible()

        selects = pw_page.locator("select")
        selects.nth(0).select_option(value=port_a["name"])
        selects.nth(1).select_option(value=port_b["name"])

        pw_page.fill("textarea", notes)
        pw_page.get_by_role("button", name="Create", exact=True).click()

        # Modal closes on success; the new connection sorts first (created_at
        # DESC, per connection_service.list_connections).
        expect(pw_page.locator("dialog[open]")).to_have_count(0)
        first_row = pw_page.locator("tbody tr").first
        expect(first_row).to_contain_text(device_a["name"])
        expect(first_row).to_contain_text(device_b["name"])

        # --- Effect assertion via the cabling API ---
        listing = _api(
            pw_page, "GET", "/cabling/connections", params={"skip": 0, "limit": 10}
        ).json()["items"]
        match = next((c for c in listing if c.get("notes") == notes), None)
        assert match is not None, "created connection not found via cabling API"
        connection_id = match["id"]
        assert match["device_a_id"] == device_a["id"]
        assert match["port_a"] == port_a["name"]
        assert match["device_b_id"] == device_b["id"]
        assert match["port_b"] == port_b["name"]

        # --- Delete via the UI ---
        row = pw_page.locator("tr", has_text=notes)
        expect(row).to_be_visible()
        row.get_by_role("button", name="Delete", exact=True).click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=notes)).to_have_count(0)

        # --- Effect assertion: gone via the API ---
        gone = _api(pw_page, "GET", f"/cabling/connections/{connection_id}", allow_errors=True)
        assert gone.status_code == 404
        connection_id = None
    finally:
        if connection_id:
            _api(pw_page, "DELETE", f"/cabling/connections/{connection_id}", allow_errors=True)
