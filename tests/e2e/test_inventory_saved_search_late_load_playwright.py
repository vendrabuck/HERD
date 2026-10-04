"""Inventory saved search that loads after mount (issue #982), in a real browser.

On a full load of /inventory the page renders before the user's preferences
arrive. A saved search that then arrives must apply to the list at once, and a
filter changed immediately afterwards must persist WITH that search. Before the
fix the loaded search went through the 300 ms search debounce, so a Status
change inside that window saved an empty search over the saved one.

The test holds the preferences GET until the page has rendered its first,
unfiltered list, releases it, waits (polling every animation frame) for the
saved search to reach the search box, and changes Status at once. It then
asserts on the server-side preference via an API read-back, not only on the UI.

The admin's `saved_filters.inventory` preference is per-user server state shared
with every other session on the same account, so the test reads the baseline
first, waits for the debounced preference PATCH carrying its final choice, and
restores the baseline in a finally block with a read-back. No devices are
created: the saved search is a per-run token that matches nothing.
"""

import json
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

PREFS_GLOB = "**/api/user-profile/preferences"


def _read_inventory_pref(page) -> dict:
    resp = pw_api(page, "GET", "/user-profile/preferences")
    return resp.json().get("saved_filters", {}).get("inventory") or {}


def _is_page_list_request(request) -> bool:
    """The page's own paginated device list GET (the all-names walker uses limit=500)."""
    parsed = urlparse(request.url)
    if request.method != "GET" or not parsed.path.endswith("/api/inventory/devices"):
        return False
    return parse_qs(parsed.query).get("limit") != ["500"]


def _search_of(request) -> str | None:
    return (parse_qs(urlparse(request.url).query).get("search") or [None])[0]


def _inventory_patch_with_status(response, status: str) -> bool:
    request = response.request
    if request.method != "PATCH" or "/user-profile/preferences" not in response.url:
        return False
    try:
        body = json.loads(request.post_data or "{}")
    except ValueError:
        return False
    inventory = (body.get("saved_filters") or {}).get("inventory")
    return isinstance(inventory, dict) and inventory.get("status") == status


def test_late_loading_saved_search_survives_an_immediate_filter_change(pw_page):
    pw_login(pw_page)
    saved_search = f"e2e-982-{uuid.uuid4().hex[:10]}"
    baseline = _read_inventory_pref(pw_page)
    held: list = []

    def hold_prefs_get(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    try:
        pw_api(
            pw_page,
            "PATCH",
            "/user-profile/preferences",
            json={"saved_filters": {"inventory": {"search": saved_search}}},
        )
        assert _read_inventory_pref(pw_page) == {"search": saved_search}

        list_searches: list[str | None] = []
        pw_page.on(
            "request",
            lambda r: list_searches.append(_search_of(r)) if _is_page_list_request(r) else None,
        )

        pw_page.route(PREFS_GLOB, hold_prefs_get)
        try:
            # A full load: the page renders and sends its first list request
            # while the preferences GET is still held.
            with pw_page.expect_request(_is_page_list_request):
                pw_page.goto(f"{HOST_BASE_URL}/inventory")
            search = pw_page.get_by_placeholder("Search devices by name...")
            status_select = pw_page.get_by_label("Status", exact=True)
            expect(status_select).to_be_visible()
            expect(search).to_have_value("")
            assert held, "the preferences GET was not held"
            assert list_searches and list_searches[0] is None

            for route in held:
                route.continue_()
            # Poll every animation frame so the change below lands well inside
            # what used to be the 300 ms debounce window.
            pw_page.wait_for_function(
                "s => document.querySelector("
                "'input[placeholder=\"Search devices by name...\"]')?.value === s",
                arg=saved_search,
                polling="raf",
            )
            with pw_page.expect_response(
                lambda r: _inventory_patch_with_status(r, "MAINTENANCE")
            ) as patch_info:
                status_select.select_option("MAINTENANCE")
        finally:
            pw_page.unroute(PREFS_GLOB, hold_prefs_get)

        sent = json.loads(patch_info.value.request.post_data or "{}")
        assert sent["saved_filters"]["inventory"] == {
            "search": saved_search,
            "status": "MAINTENANCE",
        }
        assert _read_inventory_pref(pw_page) == {"search": saved_search, "status": "MAINTENANCE"}
        # The list itself was filtered by the saved search, not only the box.
        assert saved_search in list_searches
        expect(search).to_have_value(saved_search)
        expect(pw_page.get_by_text("No devices match the current filters")).to_be_visible()

        # The saved state survives a reload.
        pw_page.reload()
        expect(pw_page.get_by_placeholder("Search devices by name...")).to_have_value(saved_search)
        expect(pw_page.get_by_label("Status", exact=True)).to_have_value("MAINTENANCE")
    finally:
        pw_api(
            pw_page,
            "PATCH",
            "/user-profile/preferences",
            json={"saved_filters": {"inventory": baseline}},
            allow_errors=True,
        )
        restored = _read_inventory_pref(pw_page)
        if restored != baseline:
            pytest.fail(f"inventory preference not restored: {restored!r} != {baseline!r}")
