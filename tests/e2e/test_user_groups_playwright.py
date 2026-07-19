"""Playwright e2e test for user group CRUD with effect assertions (issue #388 item 2).

Creates a user group through the UI, adds a member via the TransferList, then
verifies both the group and its membership through the auth service API
(never the UI's own toast/render as the assertion). Deletes the group through
the admin group list's Delete action (GroupDetailPage has no delete surface;
GroupsPage does, per frontend/src/pages/admin/GroupsPage.tsx) and confirms it
is gone via the API.

Uses the pw_page fixture (plain playwright sync API; see conftest.py's
pw_browser docstring for why this is not the pytest-playwright plugin) and
the shared pw_login helper for the main-app login form.
"""

import re
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


def _pick_member_candidate(page):
    """Pick an existing user (not the logged-in admin) to add as a group member.

    Reads the same /auth/users listing the TransferList's "Available Users"
    pane draws from, so the candidate is guaranteed to be selectable there.
    Must be called after login: it needs the browser's JWT to call the API.
    """
    me = _api(page, "GET", "/auth/me").json()
    users = _api(page, "GET", "/auth/users", params={"skip": 0, "limit": 500}).json()["items"]
    for u in users:
        if u["id"] != me["id"]:
            return u
    pytest.skip("no other user exists to add as a group member")


def test_group_create_membership_and_delete(pw_page):
    """Full UI create -> add member -> API verify -> UI delete -> API verify-gone cycle."""
    pw_login(pw_page)
    member = _pick_member_candidate(pw_page)
    group_name = f"e2e-pw-group-{int(time.time() * 1000)}"
    group_desc = "Playwright e2e test group (issue #388 item 2)"
    group_id = None

    try:
        # --- Create the group through the UI ---
        pw_page.goto(f"{HOST_BASE_URL}/admin/groups")
        pw_page.get_by_role("button", name="Create User Group").click()
        pw_page.wait_for_url("**/admin/groups/new")

        pw_page.fill("#group-name", group_name)
        pw_page.fill("#group-desc", group_desc)
        pw_page.get_by_role("button", name="Create", exact=True).click()

        # Successful create redirects to /admin/groups/{id}.
        pw_page.wait_for_url(re.compile(r".*/admin/groups/[0-9a-f-]{36}$"))
        group_id = pw_page.url.rstrip("/").rsplit("/", 1)[-1]

        # --- Add a member via the TransferList ---
        expect(pw_page.get_by_text("Members (0)")).to_be_visible()
        available_search = pw_page.locator("input[placeholder='Search...']").nth(0)
        available_search.fill(member["email"])
        expect(pw_page.get_by_text(member["username"], exact=True)).to_be_visible()

        # Exactly one row is left in the Available pane after the filter; its
        # checkbox is the only checkbox on the page (Members starts empty).
        pw_page.locator("input[type='checkbox']").first.click()
        pw_page.get_by_title(re.compile("to members")).click()
        expect(pw_page.get_by_text("Members (1)")).to_be_visible()

        # --- Effect assertion: verify via the auth API, not the UI toast ---
        detail = _api(pw_page, "GET", f"/auth/groups/{group_id}").json()
        assert detail["name"] == group_name
        assert detail["description"] == group_desc
        member_ids = {m["user_id"] for m in detail["members"]}
        assert member["id"] in member_ids

        # --- Delete via the UI (the group list page's Delete action) ---
        pw_page.goto(f"{HOST_BASE_URL}/admin/groups")
        row = pw_page.locator("tr", has_text=group_name)
        expect(row).to_be_visible()
        row.get_by_role("button", name="Delete", exact=True).click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=group_name)).to_have_count(0)

        # --- Effect assertion: gone via the API ---
        gone = _api(pw_page, "GET", f"/auth/groups/{group_id}", allow_errors=True)
        assert gone.status_code == 404
        group_id = None
    finally:
        if group_id:
            _api(pw_page, "DELETE", f"/auth/groups/{group_id}", allow_errors=True)
