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
leftover filter value would poison the inventory page for every later test.
Every search fill therefore waits for the debounced preference PATCH whose parsed
body carries that exact search (issue #1070), so no write is still in flight when
the finally block restores the baseline, and the finally block reads the
preference back and fails the test that leaked it.
"""

import uuid

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login, pw_prefs_patch_carries


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


def _search_saved(term: str):
    """Predicate for the preference PATCH that saves exactly `term` as the inventory search."""
    return lambda response: pw_prefs_patch_carries(response, "inventory", term, key="search")


def _fill_search_and_wait_for_save(page, search_box, term: str) -> None:
    """Type a search and wait for the debounced PATCH that saves exactly that term."""
    with page.expect_response(_search_saved(term)):
        search_box.fill(term)


def _restore_inventory_filter(page, baseline: dict | None) -> None:
    """Write the baseline back last, then fail the test if the read-back differs.

    `baseline` is None when it was never read (the test skipped first), in which
    case nothing was changed and nothing is written.
    """
    if baseline is None:
        return
    _api(
        page,
        "PATCH",
        "/user-profile/preferences",
        json={"saved_filters": {"inventory": baseline}},
        allow_errors=True,
    )
    after = _api(page, "GET", "/user-profile/preferences", allow_errors=True)
    if after.status_code == 200:
        restored = after.json().get("saved_filters", {}).get("inventory") or {}
        if restored != baseline:
            pytest.fail(f"inventory preference not restored: {restored!r} != {baseline!r}")


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

    baseline_inventory_filter: dict | None = None
    try:
        prefs_before = _api(pw_page, "GET", "/user-profile/preferences", allow_errors=True)
        baseline_inventory_filter = {}
        if prefs_before.status_code == 200:
            baseline_inventory_filter = (
                prefs_before.json().get("saved_filters", {}).get("inventory") or {}
            )

        pw_page.goto(f"{HOST_BASE_URL}/inventory")
        _fill_search_and_wait_for_save(
            pw_page, pw_page.get_by_placeholder("Search devices by name..."), name
        )

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
        _restore_inventory_filter(pw_page, baseline_inventory_filter)


def test_inventory_expansion_survives_delayed_search_refetch(pw_page):
    """An expanded row stays open when the debounced search refetch lands (issue #938).

    The list refetch is held with a Playwright route so the race is deterministic:
    the row is expanded while the filtered response is still in flight, then the
    response is released. Two devices share a name token so the second search
    returns a DIFFERENT id list that still contains the expanded row (the exact
    shape that used to wipe `expandedIds`); the row stays on screen during the
    hold because the page keeps the previous list as placeholder data.
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

    token = uuid.uuid4().hex[:10]
    name_a = f"e2e-pw-inv-race-{token}-a"
    name_b = f"e2e-pw-inv-race-{token}-b"
    created_ids: list[str] = []
    baseline_inventory_filter: dict | None = None
    try:
        for name in (name_a, name_b):
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
                    created_ids.append(create.json()["id"])
                    break
            else:
                pytest.skip("could not provision a test device against any device template")

        prefs_before = _api(pw_page, "GET", "/user-profile/preferences", allow_errors=True)
        baseline_inventory_filter = {}
        if prefs_before.status_code == 200:
            baseline_inventory_filter = (
                prefs_before.json().get("saved_filters", {}).get("inventory") or {}
            )

        pw_page.goto(f"{HOST_BASE_URL}/inventory")
        search_box = pw_page.get_by_placeholder("Search devices by name...")

        # Step 1: settle on a list holding only device A. The PATCH saving name_a
        # must be observed here: name_a starts with the shared token, and a
        # name_a PATCH still in flight during step 2 is what used to satisfy
        # step 2's wait early and leak the shared token (issue #1070).
        _fill_search_and_wait_for_save(pw_page, search_box, name_a)
        row_a = pw_page.locator("tbody tr", has_text=name_a)
        expect(row_a).to_have_count(1)
        expect(pw_page.locator("tbody tr", has_text=name_b)).to_have_count(0)

        # Step 2: hold the response to the shared-token search. Only a request
        # whose search term is the shared token is held, so the all-names walker
        # and every other devices call pass straight through.
        shared = f"e2e-pw-inv-race-{token}-"
        held: list = []

        def hold_shared_search(route):
            if f"search={shared}" in route.request.url:
                held.append(route)
            else:
                route.continue_()

        # The page saves the search as a debounced preference PATCH. Wait for the
        # one whose parsed body saves exactly the shared token before the cleanup
        # below restores the baseline, or that late PATCH lands after the restore
        # and poisons the saved filter for every later test (issue #1070).
        pw_page.route("**/api/inventory/devices?*", hold_shared_search)
        try:
            with (
                pw_page.expect_response(_search_saved(shared)),
                pw_page.expect_request(lambda r: f"search={shared}" in r.url),
            ):
                search_box.fill(shared)
            # Row A is still on screen from the previous list; expand it before
            # the filtered response is allowed to land.
            row_a.locator("button[aria-label*='Expand']").click()
            panel_row = row_a.locator("xpath=following-sibling::tr[1]")
            expect(panel_row.get_by_text("Modified by:", exact=True)).to_be_visible()
            assert held, "the shared-token search response was not held"
            assert pw_page.locator("tbody tr", has_text=name_b).count() == 0

            # Release the response: the list now holds both devices.
            for route in held:
                route.continue_()
        finally:
            pw_page.unroute("**/api/inventory/devices?*", hold_shared_search)

        expect(pw_page.locator("tbody tr", has_text=name_b)).to_have_count(1)
        expect(row_a).to_have_count(1)
        # The refetch kept device A, so its panel must still be open.
        expect(panel_row.get_by_text("Modified by:", exact=True)).to_be_visible()
    finally:
        for device_id in created_ids:
            _api(pw_page, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True)
        _restore_inventory_filter(pw_page, baseline_inventory_filter)
