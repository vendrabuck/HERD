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

from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login, pw_two_devices_with_ports


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
    (device_a, ports_a), (device_b, ports_b) = pw_two_devices_with_ports(pw_page)
    port_a, port_b = ports_a[0], ports_b[0]
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
        listing = pw_api(
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
        gone = pw_api(pw_page, "GET", f"/cabling/connections/{connection_id}", allow_errors=True)
        assert gone.status_code == 404
        connection_id = None
    finally:
        if connection_id:
            pw_api(pw_page, "DELETE", f"/cabling/connections/{connection_id}", allow_errors=True)
