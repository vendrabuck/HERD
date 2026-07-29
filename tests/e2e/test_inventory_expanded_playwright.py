"""Playwright port of the flaky inventory-expanded Selenium test (issue #335).

Replaces `test_inventory_expanded.py::test_inventory_expanded_shows_device_info_panel`.
Issue #335: the Selenium `logged_in_browser` fixture's fixed 15s `WebDriverWait`
login/setup occasionally overran under load (an empty-message `TimeoutException`
at setup, not an assertion failure), most recently in the 2026-07-28 nightly,
clearing on rerun both times. Playwright's locator assertions auto-wait (retry
until timeout instead of failing on the first fixed-budget check), which removes
the setup race the flake lived in.

Also upgrades the assertion from the original's loose substring check ("Created:"
in body, or "Dates"/"DATES" somewhere in body) to an effect assertion: the row's
displayed name/status/template and the expanded DeviceInfoPanel's Audit fields are
compared against a GET on the same device via the inventory API, so the test pins
actual content rather than just render.

The test provisions its own uuid-suffixed device (never touches seeded data) and
deletes it in a finally block. It also reads and restores the "inventory" key of
its saved search filter, a per-user server-side preference (user-profile service)
shared with any other concurrent session logged in as the same admin account; a
leftover filter value would poison the inventory page for another agent's run.
"""

import uuid

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


def _dummy_field_value(field: dict):
    """A schema-valid placeholder for one template field, keyed by its type."""
    ftype = field.get("type")
    if ftype == "boolean":
        return True
    if ftype == "number":
        return 1
    if ftype == "dropdown":
        options = field.get("options") or []
        return options[0] if options else "e2e-test-value"
    return "e2e-test-value"


def _required_field_data(template: dict) -> dict:
    """Minimal field_data satisfying a template's required fields.

    Only required keys are populated (optional ones are left out), and every
    key comes straight from the template's own sections, so this can never
    trip inventory's "Unknown fields" 422 regardless of which seeded template
    is picked.
    """
    data = {}
    for section in template.get("sections", []):
        for field in section.get("fields", []):
            if field.get("required"):
                data[field["key"]] = _dummy_field_value(field)
    return data


def test_inventory_expanded_shows_device_info_panel(pw_page):
    """Expanding a device row renders DeviceInfoPanel with content pinned to the API.

    Locates the row via the inventory search box (a uuid-suffixed name is unique,
    so this does not race other concurrent devices), clicks its expand chevron,
    and asserts both the row (name, status, template) and the expanded panel's
    Audit fields (created_by_name, modified_by_name) match a GET on the device
    via the inventory API.
    """
    pw_login(pw_page)

    tmpl_resp = _api(
        pw_page,
        "GET",
        "/inventory/templates",
        params={"template_type": "device", "limit": 20},
        allow_errors=True,
    )
    if tmpl_resp.status_code != 200:
        pytest.skip(f"could not list device templates: {tmpl_resp.status_code}")
    templates = tmpl_resp.json().get("items") or []
    if not templates:
        pytest.skip("no device templates available to provision a test device")

    name = f"e2e-pw-inv-expanded-{uuid.uuid4().hex[:10]}"
    device = None
    for template in templates:
        create = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": name,
                "template_id": template["id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": _required_field_data(template),
            },
            allow_errors=True,
        )
        if create.status_code == 201:
            device = create.json()
            break
    if device is None:
        pytest.skip("could not provision a test device against any available device template")
    device_id = device["id"]

    baseline_inventory_filter: dict = {}
    try:
        prefs_before = _api(pw_page, "GET", "/user-profile/preferences", allow_errors=True)
        if prefs_before.status_code == 200:
            baseline_inventory_filter = (
                prefs_before.json().get("saved_filters", {}).get("inventory") or {}
            )

        pw_page.goto(f"{HOST_BASE_URL}/inventory")
        pw_page.get_by_placeholder("Search devices by name...").fill(name)

        row = pw_page.locator("tbody tr", has_text=name)
        expect(row).to_have_count(1)

        # --- Effect assertion: row content vs the inventory API ---
        fetched = _api(pw_page, "GET", f"/inventory/devices/{device_id}").json()
        expect(row).to_contain_text(fetched["name"])
        expect(row).to_contain_text(fetched["status"])
        expect(row).to_contain_text(fetched["template_name"])

        row.locator("button[aria-label*='Expand']").click()
        panel_row = row.locator("xpath=following-sibling::tr[1]")

        expect(panel_row.get_by_text("Dates", exact=True)).to_be_visible()
        expect(panel_row.get_by_text("Created:", exact=True)).to_be_visible()
        expect(panel_row.get_by_text("Audit", exact=True)).to_be_visible()
        expect(panel_row.get_by_text("Created by:", exact=True)).to_be_visible()

        # --- Effect assertion: Audit fields vs the inventory API ---
        created_by_dd = panel_row.locator("dt", has_text="Created by:").locator(
            "xpath=following-sibling::dd"
        )
        modified_by_dd = panel_row.locator("dt", has_text="Modified by:").locator(
            "xpath=following-sibling::dd"
        )
        expect(created_by_dd).to_have_text(fetched.get("created_by_name") or "Unknown")
        expect(modified_by_dd).to_have_text(fetched.get("modified_by_name") or "Unknown")
    finally:
        _api(pw_page, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True)
        _api(
            pw_page,
            "PATCH",
            "/user-profile/preferences",
            json={"saved_filters": {"inventory": baseline_inventory_filter}},
            allow_errors=True,
        )
