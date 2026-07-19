"""Playwright e2e test for template create/duplicate/delete with effect
assertions (issue #388 item 5).

Ten Selenium template tests exist (tests/e2e/test_templates.py) but none of
them creates a template; this closes that gap. Creates a port template
(avoids the device-type driver/vendor/model requirements) with one field
through the editor UI, verifies it via the inventory API, duplicates it
through the templates list's copy action, verifies the duplicate, then
deletes both through the UI and confirms 404 for each via the API.

Templates list DESC by created_at (template_service.list_templates), so a
freshly created or duplicated template always sorts to the top of page 1;
no pagination or search is needed to find it.
"""

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


def _find_template_by_name(page, name: str) -> dict | None:
    """Look up a template by name via the first page (DESC order puts it near the top)."""
    items = _api(page, "GET", "/inventory/templates", params={"skip": 0, "limit": 20}).json()[
        "items"
    ]
    return next((t for t in items if t["name"] == name), None)


def test_template_create_duplicate_and_delete(pw_page):
    pw_login(pw_page)
    tmpl_name = f"e2e-pw-template-{int(time.time() * 1000)}"
    field_key = "e2e_field"
    field_label = "E2E Field"
    tmpl_id = None
    copy_id = None

    try:
        # --- Create a port template through the editor UI ---
        pw_page.goto(f"{HOST_BASE_URL}/templates")
        pw_page.get_by_role("button", name="Create Template").click()
        pw_page.wait_for_url("**/templates/new")

        pw_page.fill("#tmpl-name", tmpl_name)
        pw_page.select_option("#tmpl-type", value="port")

        pw_page.get_by_role("button", name="+ Add Field").first.click()
        pw_page.get_by_placeholder("Key").first.fill(field_key)
        pw_page.get_by_placeholder("Label").first.fill(field_label)

        pw_page.get_by_role("button", name="Save", exact=True).click()
        pw_page.wait_for_url(f"{HOST_BASE_URL}/templates")

        # --- Effect assertion: verify via the inventory API ---
        created = _find_template_by_name(pw_page, tmpl_name)
        assert created is not None, "created template not found via inventory API"
        tmpl_id = created["id"]
        assert created["template_type"] == "port"
        fields = created["sections"][0]["fields"]
        assert any(f["key"] == field_key and f["label"] == field_label for f in fields)

        # --- Duplicate through the templates list ---
        row = pw_page.locator("tr", has_text=tmpl_name)
        expect(row).to_be_visible()
        row.get_by_title("Duplicate template").click()

        copy_name = f"Copy of {tmpl_name}"
        expect(pw_page.locator("tr", has_text=copy_name)).to_be_visible()

        # --- Effect assertion: duplicate exists via the inventory API ---
        duplicate = _find_template_by_name(pw_page, copy_name)
        assert duplicate is not None, "duplicated template not found via inventory API"
        copy_id = duplicate["id"]
        assert duplicate["template_type"] == "port"
        dup_fields = duplicate["sections"][0]["fields"]
        assert any(f["key"] == field_key and f["label"] == field_label for f in dup_fields)

        # --- Delete both through the UI ---
        for name in (copy_name, tmpl_name):
            row = pw_page.locator("tr", has_text=name)
            expect(row).to_be_visible()
            row.get_by_role("button", name="Delete", exact=True).click()

            dialog = pw_page.locator("dialog[open]")
            expect(dialog).to_be_visible()
            dialog.get_by_role("button", name="Delete", exact=True).click()
            expect(pw_page.locator("tr", has_text=name)).to_have_count(0)

        # --- Effect assertion: both gone (404) via the API ---
        gone_original = _api(pw_page, "GET", f"/inventory/templates/{tmpl_id}", allow_errors=True)
        assert gone_original.status_code == 404
        tmpl_id = None

        gone_copy = _api(pw_page, "GET", f"/inventory/templates/{copy_id}", allow_errors=True)
        assert gone_copy.status_code == 404
        copy_id = None
    finally:
        if copy_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{copy_id}", allow_errors=True)
        if tmpl_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{tmpl_id}", allow_errors=True)
