"""Playwright e2e tests for inventory-surface mutations with effect
assertions (issue #388 items 3 and 4).

Item 4 (drivers): upload a real driver package end to end through the admin
Drivers UI (frontend/src/pages/admin/DriversPage.tsx: the Upload Driver modal
with name, connection type, and file inputs), verify it is listed via the
inventory service API, delete it through the row's Delete action and confirm
dialog, and confirm it is gone (404) via the API. The uploaded package is a
real tarball built in memory from the checked-in drivers/mock_l1 driver
(driver.py plus driver_metadata.json), matching the _tarball helper pattern
in tests/integration/test_l2_reconcile.py.

Item 3 (connections): create a backend connection end to end through the
admin Connections UI (frontend/src/pages/admin/ConnectionsPage.tsx), verify
it via the cabling service API, delete it through the row action, and confirm
it is gone via the API. Unlike the sibling test_connections_playwright.py,
which reuses two seeded devices and skips when the stack lacks them, this test
creates its own two uuid-named devices (each with a port) via the inventory
API first, so it runs regardless of seed data and never touches devices it
did not create. The device creation is setup, not the surface under test: the
UI action being exercised is the connection create/delete.

Effect-assertion discipline (issue #388): every UI mutation is verified by an
API read-back, never by the UI's own toast alone. Each finally block restores
its mutations in dependency order (connection before devices; a device delete
while a connection references it is the wrong order), so a mid-test failure
never strands resources on the shared dev stack.

Sibling coverage under issue #388: test_templates_playwright.py (item 5,
already complete) and test_connections_playwright.py (item 3, seeded-device
variant). This file adds the driver surface (item 4) and the self-contained
connection variant (item 3).
"""

import io
import tarfile
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login

MOCK_L1_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l1"


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


def _mock_l1_tarball() -> bytes:
    """Build a gzip tarball of the checked-in mock_l1 driver package."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("driver.py", "driver_metadata.json"):
            tar.add(MOCK_L1_DIR / name, arcname=name)
    return buf.getvalue()


def _find_driver_by_name(page, name: str) -> dict | None:
    """Look up a driver by name via the inventory API listing."""
    items = _api(page, "GET", "/inventory/drivers", params={"skip": 0, "limit": 100}).json()[
        "items"
    ]
    return next((d for d in items if d["name"] == name), None)


def test_driver_upload_and_delete(pw_page):
    pw_login(pw_page)
    # uuid-suffixed so the row is unique on the shared dev stack. Drivers list
    # ascending by name (driver_service.list_drivers) rather than created_at,
    # so page-1 UI visibility assumes fewer than the 50-row UI page of drivers
    # on the stack, which holds on a dev/CI stack.
    driver_name = f"e2e-pw-driver-{uuid.uuid4().hex[:12]}"
    driver_id = None

    try:
        pw_page.goto(f"{HOST_BASE_URL}/admin/drivers")
        expect(pw_page.get_by_role("button", name="Upload Driver")).to_be_visible()
        pw_page.get_by_role("button", name="Upload Driver").click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Upload Driver")

        pw_page.fill("#driver-name", driver_name)
        pw_page.fill("#driver-desc", "e2e-pw mock l1 driver")
        pw_page.select_option("#driver-connection-type", value="Layer 1 Switch")
        pw_page.locator("#driver-file").set_input_files(
            files={
                "name": "mock_l1.tar.gz",
                "mimeType": "application/gzip",
                "buffer": _mock_l1_tarball(),
            }
        )

        pw_page.get_by_role("button", name="Upload", exact=True).click()

        # Modal closes on success; the success toast confirms the UI ack.
        expect(pw_page.get_by_text("Driver uploaded")).to_be_visible()
        expect(pw_page.locator("dialog[open]")).to_have_count(0)

        # --- Effect assertion via the inventory API ---
        created = _find_driver_by_name(pw_page, driver_name)
        assert created is not None, "uploaded driver not found via inventory API"
        driver_id = created["id"]
        assert created["connection_type"] == "Layer 1 Switch"

        # --- Delete via the UI ---
        row = pw_page.locator("tr", has_text=driver_name)
        expect(row).to_be_visible()
        row.get_by_role("button", name="Delete", exact=True).click()

        confirm = pw_page.locator("dialog[open]")
        expect(confirm.locator("#confirm-dialog-title")).to_have_text("Delete Driver")
        confirm.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=driver_name)).to_have_count(0)

        # --- Effect assertion: gone (404) via the API ---
        gone = _api(pw_page, "GET", f"/inventory/drivers/{driver_id}", allow_errors=True)
        assert gone.status_code == 404
        driver_id = None
    finally:
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)


def _create_template(page, body: dict) -> dict:
    """Create a template via the inventory API; return its response dict."""
    return _api(page, "POST", "/inventory/templates", json=body).json()


def _device_template_body(driver_id: str) -> dict:
    """Minimal valid device template (needs a driver, vendor, model, a section)."""
    return {
        "name": f"e2e-pw-devtmpl-{uuid.uuid4().hex[:12]}",
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "e2e-pw",
        "model": "conn-fixture",
        "sections": [{"name": "General", "fields": []}],
    }


def _port_template_body() -> dict:
    """Minimal valid port template (no driver; one section)."""
    return {
        "name": f"e2e-pw-porttmpl-{uuid.uuid4().hex[:12]}",
        "template_type": "port",
        "sections": [{"name": "General", "fields": []}],
    }


def _create_device_with_port(page, device_template_id: str, port_template_id: str) -> dict:
    """Create a uuid-named device with one port via the inventory API; return both."""
    device = _api(
        page,
        "POST",
        "/inventory/devices",
        json={
            "name": f"e2e-pw-dev-{uuid.uuid4().hex[:12]}",
            "template_id": device_template_id,
            "topology_type": "PHYSICAL",
        },
    ).json()
    port = _api(
        page,
        "POST",
        f"/inventory/devices/{device['id']}/ports",
        json={"name": f"port-{uuid.uuid4().hex[:8]}", "template_id": port_template_id},
    ).json()
    return {"device": device, "port": port}


def _select_device(page, device_name: str) -> None:
    """Type into the next unresolved device search box and pick the match.

    Both Device A and Device B render a "Search devices..." input until picked,
    at which point that input unmounts, so `.first` targets Device A on the
    first call and Device B on the second. Mirrors the sibling
    test_connections_playwright.py helper.
    """
    search_input = page.get_by_placeholder("Search devices...").first
    search_input.fill(device_name)
    page.get_by_role("button", name=device_name, exact=True).click()


def test_connection_create_and_delete_own_devices(pw_page):
    pw_login(pw_page)

    # Fully self-contained: build the whole chain (driver, device template,
    # port template, two devices with ports) via the inventory API so the test
    # runs on an unseeded stack and never touches resources it did not create.
    # cleanup holds (method, path) pairs torn down in REVERSE dependency order
    # in the finally block: connection, devices, templates, driver.
    cleanup: list[tuple[str, str]] = []
    connection_id = None
    notes = f"e2e-pw-conn-{uuid.uuid4().hex[:12]}"

    try:
        driver = _api(
            pw_page,
            "POST",
            "/inventory/drivers",
            files={"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")},
            data={
                "name": f"e2e-pw-drv-{uuid.uuid4().hex[:12]}",
                "connection_type": "Layer 1 Switch",
            },
        ).json()
        cleanup.append(("DELETE", f"/inventory/drivers/{driver['id']}"))

        device_template = _create_template(pw_page, _device_template_body(driver["id"]))
        cleanup.append(("DELETE", f"/inventory/templates/{device_template['id']}"))
        port_template = _create_template(pw_page, _port_template_body())
        cleanup.append(("DELETE", f"/inventory/templates/{port_template['id']}"))

        created_a = _create_device_with_port(pw_page, device_template["id"], port_template["id"])
        cleanup.append(("DELETE", f"/inventory/devices/{created_a['device']['id']}"))
        created_b = _create_device_with_port(pw_page, device_template["id"], port_template["id"])
        cleanup.append(("DELETE", f"/inventory/devices/{created_b['device']['id']}"))
        device_a, port_a = created_a["device"], created_a["port"]
        device_b, port_b = created_b["device"], created_b["port"]

        # --- Create the connection through the UI ---
        pw_page.goto(f"{HOST_BASE_URL}/admin/connections")
        expect(pw_page.get_by_role("button", name="Create Connection")).to_be_visible()
        # The multi-connection dialog is the default create surface (PR
        # #538); flip to Single mode first so Create Connection opens the
        # single-pair modal this test drives, matching the sibling
        # test_connections_playwright.py pattern. This also keeps the bare
        # `select` locators below unambiguous, since only the single modal
        # is mounted in that mode.
        pw_page.get_by_role("button", name="Single", exact=True).click()
        pw_page.get_by_role("button", name="Create Connection").click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Create Connection")

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
        confirm = pw_page.locator("dialog[open]")
        expect(confirm).to_be_visible()
        confirm.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=notes)).to_have_count(0)

        # --- Effect assertion: gone via the API ---
        gone = _api(pw_page, "GET", f"/cabling/connections/{connection_id}", allow_errors=True)
        assert gone.status_code == 404
        connection_id = None
    finally:
        # Connection first (a device delete while a connection references it
        # can 409), then the tracked resources in reverse creation order
        # (devices, then templates, then driver).
        if connection_id:
            _api(pw_page, "DELETE", f"/cabling/connections/{connection_id}", allow_errors=True)
        for method, path in reversed(cleanup):
            _api(pw_page, method, path, allow_errors=True)
