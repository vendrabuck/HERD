"""Playwright e2e test for the superadmin role-change cycle (issue #388 item 8).

Promotes a seeded non-admin user to admin through the Admin > Users UI
(frontend/src/components/admin/UserManagementTable.tsx), verifies the role
via the auth users API, then demotes back and verifies again. Role changes
are superadmin-only (services/auth/app/routers/admin.py), so this logs in
with the same admin credentials the Selenium conftest treats as the seeded
superadmin (ADMIN_EMAIL/ADMIN_PASSWORD, sourced from SUPERADMIN_* in .env).

The users table has no search box, only Prev/Next pagination (limit 50), and
`make seed` creates 50 admins ahead of 1000 plain users ordered by
created_at ascending (services/auth/app/services/auth_service.py), so a
freshly created account would sort to the very last page and a fixed page
number would be wrong on a differently-sized stack. Instead the target user
is picked via the API first (any non-admin, non-self account) and then the
test pages forward through the UI until that exact username's row appears,
so it works regardless of how many users are seeded.
"""

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import ADMIN_EMAIL, ADMIN_PASSWORD, HOST_BASE_URL, pw_login

MAX_PAGES = 40


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


def _pick_non_admin(page) -> dict:
    """Pick an existing role=user account (not the logged-in superadmin) via the API."""
    me = _api(page, "GET", "/auth/me").json()
    users = _api(page, "GET", "/auth/users", params={"skip": 0, "limit": 500}).json()["items"]
    for u in users:
        if u["role"] == "user" and u["id"] != me["id"]:
            return u
    pytest.skip("no seeded non-admin user available to promote")


def _fetch_role(page, user_id: str) -> str:
    users = _api(page, "GET", "/auth/users", params={"skip": 0, "limit": 500}).json()["items"]
    match = next((u for u in users if u["id"] == user_id), None)
    assert match is not None, f"user {user_id} not found via auth API"
    return match["role"]


def _row_for_username(page, username: str):
    """Page forward through Admin > Users until the row for `username` is visible.

    Matches the username table cell EXACTLY (role "cell", exact=True) rather
    than a substring `has_text` on the row: the seeded usernames are
    user1..user1000, so a substring match on "user1" would also hit
    "user10".."user19" and "user100".."user199" and violate Playwright's
    strict mode (ambiguous locator).
    """
    for _ in range(MAX_PAGES):
        cell = page.get_by_role("cell", name=username, exact=True)
        if cell.count() > 0:
            return cell.locator("xpath=ancestor::tr[1]")
        next_btn = page.get_by_role("button", name="Next", exact=True)
        if next_btn.count() == 0 or not next_btn.is_enabled():
            break
        next_btn.click()
        page.wait_for_timeout(200)
    pytest.fail(f"could not find a row for {username} while paging Admin > Users")


def test_superadmin_promotes_then_demotes_user(pw_page):
    pw_login(pw_page, ADMIN_EMAIL, ADMIN_PASSWORD)
    target = _pick_non_admin(pw_page)
    assert target["role"] == "user"

    try:
        pw_page.goto(f"{HOST_BASE_URL}/admin/users")
        expect(pw_page.locator("table")).to_be_visible()

        # --- Promote via the UI ---
        row = _row_for_username(pw_page, target["username"])
        row.get_by_role("button", name="Promote to admin", exact=True).click()
        expect(row.get_by_role("button", name="Demote to user", exact=True)).to_be_visible()

        # --- Effect assertion via the auth API ---
        assert _fetch_role(pw_page, target["id"]) == "admin"

        # --- Demote back via the UI ---
        row = _row_for_username(pw_page, target["username"])
        row.get_by_role("button", name="Demote to user", exact=True).click()
        expect(row.get_by_role("button", name="Promote to admin", exact=True)).to_be_visible()

        # --- Effect assertion via the auth API ---
        assert _fetch_role(pw_page, target["id"]) == "user"
    finally:
        # Best-effort restore in case an assertion above failed mid-cycle.
        current = _api(
            pw_page, "GET", "/auth/users", params={"skip": 0, "limit": 500}, allow_errors=True
        )
        if current.status_code == 200:
            match = next((u for u in current.json()["items"] if u["id"] == target["id"]), None)
            if match and match["role"] != "user":
                _api(
                    pw_page,
                    "PUT",
                    f"/auth/users/{target['id']}/role",
                    json={"role": "user"},
                    allow_errors=True,
                )
