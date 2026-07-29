"""Playwright e2e tests for admin mutations, asserted by backend effect.

Issue #388 Tier 1 items 2 (group CRUD with effect) and 8 (role change). Every
UI action is confirmed by an API read-back, never by a toast or render alone,
the discipline established in test_config_playwright.py and the class of gap
issue #388 documents.

Uses the plain-Playwright pw_page fixture from conftest.py (see its pw_browser
docstring for why the pytest-playwright plugin is not used). Runs host-side
against HOST_BASE_URL.

Shared-stack contract: this stack is shared with other agents. Every resource
this module creates carries a uuid suffix, and every mutation is reverted in a
finally block. One mutation cannot be reverted: HERD's auth service has no
delete-user endpoint, so users created via /auth/register persist. They are
left as inert `user`-role accounts (role reverted, group memberships removed);
each run's users are uniquely named and harmless. The role and membership API
read-backs page through /auth/users rather than trusting a single 500-row
window, so they stay correct as that user table grows.
"""

import io
import re
import tarfile
import time
import uuid

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login

# Fresh-user password for the register/login round-trips (8 to 72 chars per the
# RegisterRequest schema). These users are throwaway and never privileged.
_FRESH_USER_PASSWORD = "pw-e2e-password"


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


def _poll(fn, predicate, *, timeout: float = 10.0, interval: float = 0.4):
    """Call fn() until predicate(result) is true or timeout elapses.

    Returns the satisfying result, or None on timeout, so an assertion on the
    return value cannot silently pass against a last-known-bad snapshot.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return None


def _register_fresh_user(page, suffix: str):
    """Register a throwaway local user, or return None if local auth is off.

    Returns {"id", "email", "username"} on success. Skips (via None) when the
    deployment uses LDAP, where /auth/register 409s by design.
    """
    username = f"pwadmin{suffix}"
    email = f"{username}@example.com"
    resp = _api(
        page,
        "POST",
        "/auth/register",
        json={"email": email, "username": username, "password": _FRESH_USER_PASSWORD},
        allow_errors=True,
    )
    if resp.status_code == 409 and "LDAP" in resp.text:
        return None
    resp.raise_for_status()
    body = resp.json()
    return {"id": body["id"], "email": email, "username": username}


def _login_token(email: str, password: str) -> str:
    """Obtain an access token for an arbitrary user via the login API."""
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.post(
            f"{HOST_BASE_URL}/api/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def _visible_device_ids(token: str) -> set[str]:
    """Device ids the given (non-admin) user can see through group visibility."""
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.get(
            f"{HOST_BASE_URL}/api/inventory/devices",
            params={"limit": 500},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return {d["id"] for d in resp.json()["items"]}


def _find_user(page, user_id: str):
    """Return the user row for user_id by paging all of /auth/users, or None.

    Pages rather than trusting a single 500-row window: the auth user list is
    ordered oldest-first and the accumulating throwaway users push freshly
    created ones past a single page.
    """
    skip = 0
    limit = 500
    while True:
        body = _api(page, "GET", "/auth/users", params={"skip": skip, "limit": limit}).json()
        for user in body["items"]:
            if user["id"] == user_id:
                return user
        skip += limit
        if skip >= body["total"]:
            return None


def _mgmt_driver_tarball() -> bytes:
    """A minimal no-op Management driver, enough to back a DUT template."""
    body = b"class Driver:\n    pass\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _transfer_move(scope, item_text: str, button_title_fragment: str, search_index: int) -> None:
    """Move one TransferList item across the panes.

    search_index selects which pane's search box to filter (0 = left/available,
    1 = right/assigned); button_title_fragment is the lowercased destination
    label carried in the move button's title (see components/ui/TransferList).
    """
    searches = scope.get_by_placeholder("Search...")
    searches.nth(search_index).fill(item_text)
    row = scope.locator("label", has_text=item_text)
    expect(row).to_be_visible()
    row.get_by_role("checkbox").check()
    scope.locator(f'button[title*="{button_title_fragment}"]').click()


def _device_group_id_from_url(page) -> str:
    page.wait_for_url(re.compile(r".*/admin/device-groups/[0-9a-fA-F-]{36}"))
    return page.url.rstrip("/").rsplit("/", 1)[-1]


def _group_id_from_url(page) -> str:
    page.wait_for_url(re.compile(r".*/admin/groups/[0-9a-fA-F-]{36}"))
    return page.url.rstrip("/").rsplit("/", 1)[-1]


def test_user_group_membership_effect(pw_page):
    """Create a user group via the UI, add a member via the transfer list.

    Effect asserted: GET /auth/groups/{id} members list gains, then loses, the
    added user's id. Reuses an existing non-superadmin user (guaranteed inside
    the transfer list's oldest-first fetch window) rather than a fresh one, so
    the add is exercised through the real transfer-list UI.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]

    users = _api(pw_page, "GET", "/auth/users", params={"limit": 500}).json()["items"]
    candidates = [u for u in users if u["role"] != "superadmin"]
    if not candidates:
        pytest.skip("no non-superadmin user to add as a group member")
    member = candidates[0]

    group_id = None
    try:
        pw_page.goto(f"{HOST_BASE_URL}/admin/groups/new")
        pw_page.fill("#group-name", f"pw-ug-{suffix}")
        pw_page.get_by_role("button", name="Create", exact=True).click()
        group_id = _group_id_from_url(pw_page)

        # Members TransferList renders once the created group loads.
        expect(pw_page.get_by_text(re.compile(r"Members \(\d+\)"))).to_be_visible()

        _transfer_move(pw_page, member["email"], "members", search_index=0)
        added = _poll(
            lambda: _api(pw_page, "GET", f"/auth/groups/{group_id}").json(),
            lambda g: any(m["user_id"] == member["id"] for m in g["members"]),
        )
        assert added is not None, "member never appeared in the group via the auth API"

        _transfer_move(pw_page, member["email"], "available users", search_index=1)
        removed = _poll(
            lambda: _api(pw_page, "GET", f"/auth/groups/{group_id}").json(),
            lambda g: all(m["user_id"] != member["id"] for m in g["members"]),
        )
        assert removed is not None, "member was never removed from the group via the auth API"
    finally:
        if group_id:
            _api(pw_page, "DELETE", f"/auth/groups/{group_id}", allow_errors=True)


def test_device_group_visibility_effect(pw_page):
    """Assign a device to a device group via the UI, assert the visibility effect.

    A fresh non-admin user is placed (via API) in a user group that permissions
    an empty device group. The causal UI action under test is assigning, then
    removing, a DUT device through the device group's transfer list; the effect
    is asserted as the non-admin user's device list gaining, then losing, that
    device id. Setup around the causal action is seeded via API to isolate it.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]

    user_group_id = None
    device_group_id = None
    driver_id = None
    template_id = None
    device_id = None
    try:
        user = _register_fresh_user(pw_page, suffix)
        if user is None:
            pytest.skip("local registration disabled (LDAP deployment)")
        token = _login_token(user["email"], _FRESH_USER_PASSWORD)

        # User group with the fresh user as its only member (seeded via API).
        user_group_id = _api(
            pw_page, "POST", "/auth/groups", json={"name": f"pw-vis-ug-{suffix}"}
        ).json()["id"]
        _api(
            pw_page,
            "POST",
            f"/auth/groups/{user_group_id}/members",
            json={"user_id": user["id"]},
        )

        # A DUT: a device whose template's driver is a Management connection.
        driver = _api(
            pw_page,
            "POST",
            "/inventory/drivers",
            files={"file": ("mgmt.tar.gz", _mgmt_driver_tarball(), "application/gzip")},
            data={
                "name": f"pw-vis-driver-{suffix}",
                "connection_type": "Management",
                "description": "playwright visibility test DUT driver",
            },
        ).json()
        driver_id = driver["id"]
        template = _api(
            pw_page,
            "POST",
            "/inventory/templates",
            json={
                "name": f"pw-vis-tmpl-{suffix}",
                "template_type": "device",
                "driver_id": driver_id,
                "vendor": "PlaywrightVendor",
                "model": "DUT",
                "sections": [
                    {
                        "name": "General",
                        "fields": [{"key": "model", "label": "Model", "type": "string"}],
                    }
                ],
            },
        ).json()
        template_id = template["id"]
        device = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-vis-dev-{suffix}",
                "template_id": template_id,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "test"},
            },
        ).json()
        device_id = device["id"]

        # Device group via the UI.
        pw_page.goto(f"{HOST_BASE_URL}/admin/device-groups/new")
        pw_page.fill("#dg-name", f"pw-vis-dg-{suffix}")
        pw_page.get_by_role("button", name="Create", exact=True).click()
        device_group_id = _device_group_id_from_url(pw_page)

        # Permission the user group onto the (still empty) device group via API.
        _api(
            pw_page,
            "POST",
            f"/inventory/device-groups/{device_group_id}/permissions/bulk",
            json={"user_group_ids": [user_group_id]},
        )

        # Baseline: empty device group means the user sees nothing yet.
        assert device_id not in _visible_device_ids(token), (
            "device visible before it was assigned to the group"
        )

        # UI: assign the device through the transfer-list modal, then remove it
        # in the same open modal (the assigned pane updates live on refetch).
        pw_page.get_by_role("button", name="Add or Remove Devices").click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog).to_be_visible()

        _transfer_move(dialog, device["name"], "group devices", search_index=0)
        gained = _poll(lambda: _visible_device_ids(token), lambda ids: device_id in ids)
        assert gained is not None, "assigning the device did not make it visible to the user"

        # The device now sits in the assigned pane; move it back to revoke.
        _transfer_move(dialog, device["name"], "available devices", search_index=1)
        lost = _poll(lambda: _visible_device_ids(token), lambda ids: device_id not in ids)
        assert lost is not None, "removing the device did not revoke visibility for the user"
    finally:
        if device_group_id:
            _api(
                pw_page,
                "DELETE",
                f"/inventory/device-groups/{device_group_id}",
                allow_errors=True,
            )
        if device_id:
            _api(pw_page, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True)
        if template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        if user_group_id:
            _api(pw_page, "DELETE", f"/auth/groups/{user_group_id}", allow_errors=True)


def _go_to_last_users_page(page) -> None:
    """Page the user table to its last page (newest users live there).

    The auth list is oldest-first, so a freshly registered user is the final
    row on the final page. No pagination controls render when a single page
    covers everyone (Pagination returns null at total <= limit).
    """
    indicator = page.get_by_text(re.compile(r"Page \d+ of \d+"))
    if indicator.count() == 0:
        return
    while True:
        text = indicator.inner_text()
        match = re.search(r"Page (\d+) of (\d+)", text)
        if not match or match.group(1) == match.group(2):
            return
        page.get_by_role("button", name="Next", exact=True).click()
        expect(indicator).not_to_have_text(text)


def test_role_promote_demote_effect(pw_page):
    """Superadmin promotes then demotes a user via the UI; role asserted via API.

    Effect asserted: GET /auth/users shows the target user's role flip to
    `admin`, then back to `user`, after each UI click. The target is a fresh
    throwaway user so no real account's role is disturbed.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]

    user = _register_fresh_user(pw_page, suffix)
    if user is None:
        pytest.skip("local registration disabled (LDAP deployment)")

    try:
        pw_page.goto(f"{HOST_BASE_URL}/admin/users")
        # Wait for the user table to finish loading before reading pagination,
        # otherwise the async fetch has not rendered the page indicator yet.
        expect(pw_page.locator("tbody tr").first).to_be_visible()
        _go_to_last_users_page(pw_page)

        row = pw_page.locator("tr", has_text=user["email"])
        expect(row).to_be_visible()
        row.get_by_role("button", name="Promote to admin").click()
        promoted = _poll(
            lambda: _find_user(pw_page, user["id"]),
            lambda u: u is not None and u["role"] == "admin",
        )
        assert promoted is not None, "user role never became admin via the users API"

        row = pw_page.locator("tr", has_text=user["email"])
        expect(row).to_be_visible()
        row.get_by_role("button", name="Demote to user").click()
        demoted = _poll(
            lambda: _find_user(pw_page, user["id"]),
            lambda u: u is not None and u["role"] == "user",
        )
        assert demoted is not None, "user role never returned to user via the users API"
    finally:
        # Guarantee the throwaway user is left unprivileged even on mid-test failure.
        _api(
            pw_page,
            "PUT",
            f"/auth/users/{user['id']}/role",
            json={"role": "user"},
            allow_errors=True,
        )
