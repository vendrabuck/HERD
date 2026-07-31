"""Playwright e2e test for dynamic (hypervisor-backed) template authoring
(issue #398): the admin Hypervisors page plus the widened TemplateEditorPage
type union.

ADR 0004 decision point 6 shipped the dynamic template type API-only; #398
closes the UI gap. This test exercises both new surfaces end to end through
real backend effects rather than UI acknowledgment alone:

1. Registers a hypervisor through the new admin Hypervisors page
   (frontend/src/pages/admin/HypervisorsPage.tsx), backed by a real secret
   and verified via the inventory hypervisors API.
2. Edits that hypervisor through the same page and verifies the change.
3. Creates a dynamic template through TemplateEditorPage, picking a
   Hypervisor-type recipe driver and the registered hypervisor, verified via
   the inventory templates API (template_type, driver_id, hypervisor_id).
4. Deletes the template and the hypervisor through their respective admin
   UIs and confirms both are gone (404) via the API.

A driver upload and a secret are prerequisites with no dedicated e2e surface
of their own yet (secrets authoring is API-only; driver upload is covered by
tests/e2e/test_drivers.py), so both are created directly via the API, mirroring
the transient_driver fixture pattern in conftest.py but with the Hypervisor
connection type transient_driver does not offer.

Full effect-assertion + cleanup discipline per docs/MANUAL_TESTING.md's
"Explicitly automated instead" note and the #388 worked example
(test_templates_playwright.py): every UI mutation is confirmed via a direct
API read-back, and the finally block tears down every resource this test
creates, in dependency order (template, then hypervisor, then driver and
secret), tolerating partial failure with allow_errors.
"""

import io
import tarfile
import time

import httpx
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


def _create_hypervisor_driver(page, name: str) -> dict:
    """Upload a minimal Hypervisor-connection-type driver package via the API.

    Structurally identical to conftest.transient_driver, but that fixture is
    pinned to connection_type "Management"; a dynamic template's driver must
    be Hypervisor-type (template_service._validate_driver_connection_type),
    so this test needs its own upload rather than the shared fixture.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        driver_py = b"class Driver:\n    def status(self, *a, **k):\n        return {}\n"
        info = tarfile.TarInfo(name="driver.py")
        info.size = len(driver_py)
        tar.addfile(info, io.BytesIO(driver_py))
    buf.seek(0)

    files = {"file": (f"{name}.tar.gz", buf.getvalue(), "application/gzip")}
    data = {
        "name": name,
        "description": "transient e2e hypervisor recipe driver",
        "connection_type": "Hypervisor",
    }
    resp = _api(page, "POST", "/inventory/drivers", files=files, data=data)
    return resp.json()


def _create_secret(page, name: str) -> dict:
    body = {
        "name": name,
        "type": "password",
        "description": "transient e2e hypervisor credential",
        "data": {"password": "e2e-dummy-not-a-real-secret"},
    }
    resp = _api(page, "POST", "/secrets/secrets", json=body)
    return resp.json()


def test_hypervisor_registration_and_dynamic_template_authoring(pw_page):
    pw_login(pw_page)
    suffix = int(time.time() * 1000)
    driver_name = f"e2e-pw-hv-driver-{suffix}"
    secret_name = f"e2e-pw-hv-secret-{suffix}"
    hv_name = f"e2e-pw-hypervisor-{suffix}"
    tmpl_name = f"e2e-pw-dynamic-template-{suffix}"

    driver_id = None
    secret_id = None
    hypervisor_id = None
    template_id = None

    try:
        # --- Prerequisites: a Hypervisor-type driver and a secret ---
        driver = _create_hypervisor_driver(pw_page, driver_name)
        driver_id = driver["id"]
        secret = _create_secret(pw_page, secret_name)
        secret_id = secret["id"]

        # --- Register a hypervisor through the new admin Hypervisors page ---
        pw_page.goto(f"{HOST_BASE_URL}/admin/hypervisors")
        pw_page.get_by_role("button", name="Register Hypervisor").click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Register Hypervisor")

        pw_page.fill("#hv-name", hv_name)
        pw_page.fill("#hv-endpoint", "https://e2e-proxmox.example.local:8006")
        pw_page.fill("#hv-type", "proxmox")
        pw_page.select_option("#hv-secret", value=secret_id)
        pw_page.get_by_role("button", name="Register", exact=True).click()

        expect(pw_page.locator("dialog[open]")).to_have_count(0)
        expect(pw_page.locator("tr", has_text=hv_name)).to_be_visible()

        # --- Effect assertion: hypervisor exists via the inventory API ---
        listing = _api(
            pw_page, "GET", "/inventory/hypervisors", params={"skip": 0, "limit": 100}
        ).json()["items"]
        match = next((h for h in listing if h["name"] == hv_name), None)
        assert match is not None, "registered hypervisor not found via inventory API"
        hypervisor_id = match["id"]
        assert match["hypervisor_type"] == "proxmox"
        assert match["secret_id"] == secret_id
        assert match["enabled"] is True

        # --- Edit the hypervisor through the same page ---
        row = pw_page.locator("tr", has_text=hv_name)
        row.get_by_role("button", name="Edit", exact=True).click()
        edit_dialog = pw_page.locator("dialog[open]")
        expect(edit_dialog.locator("#modal-title")).to_have_text("Edit Hypervisor")
        pw_page.uncheck("#hv-enabled")
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.locator("dialog[open]")).to_have_count(0)

        # --- Effect assertion: edit landed via the inventory API ---
        updated = _api(pw_page, "GET", f"/inventory/hypervisors/{hypervisor_id}").json()
        assert updated["enabled"] is False

        # --- Create a dynamic template through the editor UI ---
        pw_page.goto(f"{HOST_BASE_URL}/templates/new")
        pw_page.fill("#tmpl-name", tmpl_name)
        pw_page.select_option("#tmpl-type", value="dynamic")

        # Hardware Identity (vendor/model) must NOT be required for dynamic.
        expect(pw_page.get_by_text("Hardware Identity")).to_have_count(0)

        pw_page.select_option("#tmpl-driver", value=driver_id)
        pw_page.select_option("#tmpl-hypervisor", value=hypervisor_id)

        pw_page.get_by_role("button", name="+ Add Field").first.click()
        pw_page.get_by_placeholder("Key").first.fill("image")
        pw_page.get_by_placeholder("Label").first.fill("Image")

        pw_page.get_by_role("button", name="Save", exact=True).click()
        pw_page.wait_for_url(f"{HOST_BASE_URL}/templates")

        # --- Effect assertion: template exists via the inventory API ---
        tmpl_listing = _api(
            pw_page, "GET", "/inventory/templates", params={"skip": 0, "limit": 20}
        ).json()["items"]
        tmpl_match = next((t for t in tmpl_listing if t["name"] == tmpl_name), None)
        assert tmpl_match is not None, "created dynamic template not found via inventory API"
        template_id = tmpl_match["id"]
        assert tmpl_match["template_type"] == "dynamic"
        assert tmpl_match["driver_id"] == driver_id
        assert tmpl_match["hypervisor_id"] == hypervisor_id
        fields = tmpl_match["sections"][0]["fields"]
        assert any(f["key"] == "image" and f["label"] == "Image" for f in fields)

        # --- Delete the template through the Templates list UI ---
        tmpl_row = pw_page.locator("tr", has_text=tmpl_name)
        expect(tmpl_row).to_be_visible()
        tmpl_row.get_by_role("button", name="Delete", exact=True).click()
        confirm = pw_page.locator("dialog[open]")
        expect(confirm).to_be_visible()
        confirm.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=tmpl_name)).to_have_count(0)

        gone_tmpl = _api(pw_page, "GET", f"/inventory/templates/{template_id}", allow_errors=True)
        assert gone_tmpl.status_code == 404
        template_id = None

        # --- Delete the hypervisor through the Hypervisors page UI ---
        pw_page.goto(f"{HOST_BASE_URL}/admin/hypervisors")
        hv_row = pw_page.locator("tr", has_text=hv_name)
        expect(hv_row).to_be_visible()
        hv_row.get_by_role("button", name="Delete", exact=True).click()
        hv_confirm = pw_page.locator("dialog[open]")
        expect(hv_confirm).to_be_visible()
        hv_confirm.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=hv_name)).to_have_count(0)

        gone_hv = _api(pw_page, "GET", f"/inventory/hypervisors/{hypervisor_id}", allow_errors=True)
        assert gone_hv.status_code == 404
        hypervisor_id = None
    finally:
        if template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if hypervisor_id:
            _api(pw_page, "DELETE", f"/inventory/hypervisors/{hypervisor_id}", allow_errors=True)
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        if secret_id:
            _api(pw_page, "DELETE", f"/secrets/secrets/{secret_id}", allow_errors=True)


def test_device_template_creation_still_works_with_dynamic_option_present(pw_page):
    """Regression guard for the widened type union: the device flow (the
    common case, unaffected by #398) must remain byte-identical in behavior
    now that a third "dynamic" option and its conditional fields exist.
    """
    pw_login(pw_page)
    driver_name = f"e2e-pw-device-driver-{int(time.time() * 1000)}"
    tmpl_name = f"e2e-pw-device-template-{int(time.time() * 1000)}"
    driver_id = None
    template_id = None

    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            driver_py = b"class Driver:\n    def status(self, *a, **k):\n        return {}\n"
            info = tarfile.TarInfo(name="driver.py")
            info.size = len(driver_py)
            tar.addfile(info, io.BytesIO(driver_py))
        buf.seek(0)
        driver_resp = _api(
            pw_page,
            "POST",
            "/inventory/drivers",
            files={"file": (f"{driver_name}.tar.gz", buf.getvalue(), "application/gzip")},
            data={"name": driver_name, "connection_type": "Management"},
        )
        driver_id = driver_resp.json()["id"]

        pw_page.goto(f"{HOST_BASE_URL}/templates/new")
        pw_page.fill("#tmpl-name", tmpl_name)
        # "device" is the default; select it explicitly for clarity/robustness.
        pw_page.select_option("#tmpl-type", value="device")
        pw_page.select_option("#tmpl-driver", value=driver_id)
        pw_page.fill("#tmpl-vendor", "E2E Vendor")
        pw_page.fill("#tmpl-model", "E2E Model")

        pw_page.get_by_role("button", name="Save", exact=True).click()
        pw_page.wait_for_url(f"{HOST_BASE_URL}/templates")

        tmpl_listing = _api(
            pw_page, "GET", "/inventory/templates", params={"skip": 0, "limit": 20}
        ).json()["items"]
        match = next((t for t in tmpl_listing if t["name"] == tmpl_name), None)
        assert match is not None, "created device template not found via inventory API"
        template_id = match["id"]
        assert match["template_type"] == "device"
        assert match["driver_id"] == driver_id
        assert match["hypervisor_id"] is None
        assert match["vendor"] == "E2E Vendor"
        assert match["model"] == "E2E Model"
    finally:
        if template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
