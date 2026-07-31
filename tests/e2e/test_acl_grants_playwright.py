"""Playwright e2e test for the ACL grants management UI (issue #397).

Creates an ACL grant through the Grants page (`/admin/grants`), verifies it
through the acl service's own API read-back (never the UI toast or render
alone), then deletes it through the UI and verifies it is gone. Follows the
effect-assertion discipline established by test_config_playwright.py and
test_user_groups_playwright.py.

Uses the pw_page fixture (plain playwright sync API; see conftest.py's
pw_browser docstring for why this is not the pytest-playwright plugin) and
the shared pw_login helper for the main-app login form.

Shared-stack contract: this stack is shared with other agents. The user group
this test creates carries a uuid suffix and is deleted in a finally block; the
grant it creates against an existing device is deleted the same way. The
device itself is only read, never mutated.

NOT RUN by this agent (no Docker stack available in this worktree). Requires
the live gate (`make test-e2e`) to execute.
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


def _pick_resource_device(page):
    """Pick an existing device to use as the grant's resource_id.

    Read-only: the device is only referenced by id, never mutated.
    """
    devices = _api(page, "GET", "/inventory/devices", params={"skip": 0, "limit": 1}).json()
    items = devices.get("items", devices) if isinstance(devices, dict) else devices
    if not items:
        pytest.skip("no device exists to use as a grant resource")
    return items[0]


def test_grant_create_effect_and_delete(pw_page):
    """Full UI create -> API verify -> UI delete -> API verify-gone cycle."""
    pw_login(pw_page)
    device = _pick_resource_device(pw_page)
    suffix = int(time.time() * 1000)
    group_name = f"e2e-pw-grant-group-{suffix}"
    group_id = None
    grant_id = None

    try:
        # --- Seed the group via the auth API (group CRUD is its own tested
        # surface; this test is about the grants UI, not group creation) ---
        group = _api(
            pw_page,
            "POST",
            "/auth/groups",
            json={"name": group_name, "description": "Playwright ACL grant e2e (issue #397)"},
        ).json()
        group_id = group["id"]

        # --- Create the grant through the UI ---
        pw_page.goto(f"{HOST_BASE_URL}/admin/grants")
        pw_page.get_by_role("button", name="Create Grant").click()

        dialog = pw_page.get_by_role("dialog", name="Create Grant")
        expect(dialog).to_be_visible()

        dialog.locator("#grant-group").select_option(group_id)
        dialog.locator("#grant-resource-type").select_option("device")
        dialog.locator("#grant-resource-id").fill(device["id"])
        dialog.locator("#grant-permission").select_option("manage")
        dialog.get_by_role("button", name="Create", exact=True).click()

        expect(dialog).to_be_hidden()

        # The group name also appears in the group filter <option> and in the
        # (mounted-but-closed) create modal's own <option>, so a bare
        # get_by_text(group_name) is a Playwright strict-mode violation
        # (matches 3 elements). Scope to the table row instead. Filtering the
        # list to this throwaway group also sidesteps a shared-stack ordering
        # trap: list_grants orders by granted_at ASC, so on a stack with more
        # than a page of grants the new row would otherwise land past the
        # first page and the row locator below would never see it.
        pw_page.locator("#grant-filter-group").select_option(group_id)
        row = pw_page.locator("tr", has_text=group_name)
        expect(row).to_be_visible()

        # --- Effect assertion: verify via the acl service API, not the UI ---
        matching = _api(
            pw_page,
            "GET",
            "/acl/grants",
            params={"group_id": group_id, "resource_type": "device", "resource_id": device["id"]},
        ).json()
        assert matching["total"] == 1
        grant = matching["items"][0]
        grant_id = grant["id"]
        assert grant["permission"] == "manage"
        assert grant["group_id"] == group_id
        assert grant["resource_id"] == device["id"]

        # --- Delete via the UI (the grants table's row action) ---
        row.get_by_role("button", name="Delete", exact=True).click()

        confirm_dialog = pw_page.get_by_role("dialog", name="Delete Grant")
        expect(confirm_dialog).to_be_visible()
        confirm_dialog.get_by_role("button", name="Delete", exact=True).click()
        expect(pw_page.locator("tr", has_text=group_name)).to_have_count(0)

        # --- Effect assertion: gone via the API ---
        gone = _api(pw_page, "GET", f"/acl/grants/{grant_id}", allow_errors=True)
        assert gone.status_code == 404
        grant_id = None
    finally:
        if grant_id:
            _api(pw_page, "DELETE", f"/acl/grants/{grant_id}", allow_errors=True)
        if group_id:
            _api(pw_page, "DELETE", f"/auth/groups/{group_id}", allow_errors=True)


def test_grants_page_blocked_for_non_admin(pw_page):
    """A non-admin lands back on /topology, mirroring the other admin pages' guard.

    pw_page is a fresh browser context per test (see conftest.py's pw_page
    fixture), so logging in as a throwaway user here has no effect on any
    other test's session.

    The auth service exposes no user-delete endpoint, so the registered
    account cannot be torn down; it uses a unique timestamped username and
    email so it stays inert, the same mitigation test_register_and_roles.py
    documents for its own throwaway registration.
    """
    suffix = int(time.time() * 1000)
    username = f"e2epwgrant{suffix}"[:32]
    email = f"{username}@example.com"
    password = "pw-e2e-password"

    # Register a fresh, ordinary user (no privilege escalation available via
    # the register endpoint) and confirm the Grants page redirects it away.
    with httpx.Client(base_url=HOST_BASE_URL, verify=False, timeout=15.0) as client:
        resp = client.post(
            "/api/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        if resp.status_code not in (200, 201):
            pytest.skip(f"could not register a throwaway user: {resp.status_code} {resp.text}")

    pw_login(pw_page, email=email, password=password)
    pw_page.goto(f"{HOST_BASE_URL}/admin/grants")
    pw_page.wait_for_url("**/topology**")
