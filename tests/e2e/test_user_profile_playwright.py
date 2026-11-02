"""Playwright e2e tests for the user profile (issue #1252).

Two flows, each asserting the backend-observable effect through the API after
the UI action and restoring the account's baseline in a finally:

- the signed-in account edits its own profile on Settings, read back through
  GET /auth/me;
- an admin edits another account's profile from Admin > Users, read back
  through GET /auth/users.

Each save is ordered by the PATCH's own response (pw_click_for_response),
never by the "Profile saved" toast, which an earlier save could have raised.
Both flows reuse existing accounts (the signed-in superadmin, and a seeded
role=user account picked through the API), because auth has no user-delete
endpoint and a registered throwaway account would persist on the stack.
"""

import re
import uuid

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_click_for_response, pw_login

MAX_PAGES = 40
_PROFILE_KEYS = ("full_name", "contact_email", "phone")


def _profile_of(user: dict) -> dict:
    return {key: user[key] for key in _PROFILE_KEYS}


def _find_user(page, user_id: str) -> dict:
    skip = 0
    while True:
        body = pw_api(page, "GET", "/auth/users", params={"skip": skip, "limit": 500}).json()
        for item in body["items"]:
            if item["id"] == user_id:
                return item
        skip += len(body["items"])
        if not body["items"] or skip >= body["total"]:
            raise AssertionError(f"user {user_id} not found via GET /auth/users")


def _pick_non_admin(page, self_id: str) -> dict:
    body = pw_api(page, "GET", "/auth/users", params={"skip": 0, "limit": 500}).json()
    for item in body["items"]:
        if item["role"] == "user" and item["id"] != self_id and item["is_active"]:
            return item
    pytest.skip("no seeded non-admin user available to edit")


def _edit_button_for(page, username: str):
    """Page forward through Admin > Users until that account's Edit profile
    control is visible (the list is oldest-first, 50 per page).

    Each Next is ordered by the list GET it causes, never a sleep: the table
    keeps the previous page on screen while the next one loads
    (usePaginatedUsers uses keepPreviousData), so a fixed wait can read the old
    page. After the response, the indicator text changing proves the new page
    rendered before the control is counted.
    """
    name = f"Edit profile of {username}"
    for _ in range(MAX_PAGES):
        button = page.get_by_role("button", name=name, exact=True)
        if button.count() > 0:
            return button
        next_btn = page.get_by_role("button", name="Next", exact=True)
        if next_btn.count() == 0 or not next_btn.is_enabled():
            break
        indicator = page.get_by_text(re.compile(r"Page \d+ of \d+"))
        before = indicator.inner_text()
        with page.expect_response(
            lambda r: (
                r.request.method == "GET"
                and r.url.split("?", 1)[0].endswith("/api/auth/users")
                and r.status != 401
            )
        ) as info:
            next_btn.click()
        assert info.value.status == 200, info.value.text()
        expect(indicator).not_to_have_text(before)
    pytest.fail(f"could not find the Edit profile control for {username} on Admin > Users")


def test_self_profile_edit_on_settings_reads_back_through_me(pw_page):
    pw_login(pw_page)
    before = pw_api(pw_page, "GET", "/auth/me").json()
    baseline = _profile_of(before)
    suffix = uuid.uuid4().hex[:8]
    wanted = {
        "full_name": f"E2E Self {suffix}",
        "contact_email": f"e2e-self-{suffix}@mail.example.com",
        "phone": "+1 (555) 010-0150",
    }
    try:
        pw_page.goto(f"{HOST_BASE_URL}/settings")
        expect(pw_page.get_by_role("heading", name="Profile", exact=True)).to_be_visible()
        name = pw_page.get_by_label("Name", exact=True)
        expect(name).to_have_value(before["full_name"])
        name.fill(wanted["full_name"])
        pw_page.get_by_label("Contact email", exact=True).fill(wanted["contact_email"])
        pw_page.get_by_label("Phone", exact=True).fill(wanted["phone"])

        response = pw_click_for_response(
            pw_page,
            pw_page.get_by_role("button", name="Save profile", exact=True),
            "PATCH",
            "/api/auth/me/profile",
        )
        assert _profile_of(response.json()) == wanted

        after = pw_api(pw_page, "GET", "/auth/me").json()
        assert _profile_of(after) == wanted
        assert after["modified_by"] == before["id"]
        # The header shows the new name once the cached user is replaced.
        expect(
            pw_page.locator("header").get_by_text(wanted["full_name"], exact=True)
        ).to_be_visible()
    finally:
        restore = pw_api(pw_page, "PATCH", "/auth/me/profile", json=baseline, allow_errors=True)
        assert restore.status_code == 200, restore.text
        assert _profile_of(pw_api(pw_page, "GET", "/auth/me").json()) == baseline


def test_admin_edits_another_users_profile_on_the_users_page(pw_page):
    pw_login(pw_page)
    me = pw_api(pw_page, "GET", "/auth/me").json()
    target = _pick_non_admin(pw_page, me["id"])
    baseline = _profile_of(target)
    suffix = uuid.uuid4().hex[:8]
    new_name = f"E2E Edited {suffix}"
    new_phone = "555 0151"
    try:
        pw_page.goto(f"{HOST_BASE_URL}/admin/users")
        expect(pw_page.locator("tbody tr").first).to_be_visible()
        _edit_button_for(pw_page, target["username"]).click()

        dialog = pw_page.get_by_role("dialog")
        expect(dialog.get_by_role("heading", name="Edit profile", exact=True)).to_be_visible()
        expect(dialog.get_by_label("Name", exact=True)).to_have_value(target["full_name"])
        dialog.get_by_label("Name", exact=True).fill(new_name)
        dialog.get_by_label("Phone", exact=True).fill(new_phone)

        response = pw_click_for_response(
            pw_page,
            dialog.get_by_role("button", name="Save profile", exact=True),
            "PATCH",
            f"/api/auth/users/{target['id']}/profile",
        )
        assert response.json()["id"] == target["id"]

        listed = _find_user(pw_page, target["id"])
        assert _profile_of(listed) == {
            "full_name": new_name,
            "contact_email": baseline["contact_email"],
            "phone": new_phone,
        }
        assert listed["modified_by"] == me["id"]
        expect(pw_page.get_by_role("dialog")).to_have_count(0)
    finally:
        restore = pw_api(
            pw_page,
            "PATCH",
            f"/auth/users/{target['id']}/profile",
            json=baseline,
            allow_errors=True,
        )
        assert restore.status_code == 200, restore.text
        assert _profile_of(_find_user(pw_page, target["id"])) == baseline
