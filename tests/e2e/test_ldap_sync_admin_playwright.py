"""Playwright e2e test for the LDAP directory sync admin UI (ADR 0011 phase 6).

Follows the effect-assertion discipline established by test_config_playwright.py
and test_acl_grants_playwright.py: UI state is checked against the backend's own
API read-back, not just what rendered.

The e2e stack always boots with AUTH_METHOD=local, and this file's own e2e
phase (`make test-e2e`) runs BEFORE the gate ever switches auth to LDAP mode
(see the Makefile's _gate-ldap-stack-tests, issue #572, which runs after e2e
and restores local mode before any later phase), so within this file's own
phase there is no reachable LDAP-mode stack to test against: every scenario
here is the LOCAL-MODE surface: the mode banner, the create-mapping and
sync-now buttons disabled, and the mappings/runs lists (history-proofed
against the API's own totals rather than assumed empty). The ldap-mode UI
flows (enabled buttons, interval text, create-with-warning banner, sync-now
polling, run status badges, staleness handling) are covered at vitest level
only (frontend/src/test/pages/LdapSyncPage.test.tsx and
frontend/src/test/api/ldapSync.test.ts), not by Playwright anywhere: the
_gate-ldap-stack-tests phase that does put the stack in LDAP mode exercises
the sync ADMIN surface through tests/integration/test_ldap_sync_admin.py
(the public API directly), not through this UI. Live-LDAP coverage of the
sync mechanics themselves also lives in services/auth/tests (gate-required,
per ADR 0011's phase 1-5 testing section).

Uses the pw_page fixture (plain playwright sync API) and the shared pw_login
helper for the main-app login form.

NOT RUN by this agent (no Docker stack available in this worktree, and this
agent must not touch the running stack). Requires the live gate
(`make test-e2e`) to execute.
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


def _assert_section_matches_total(page, heading_text: str, empty_text: str, total: int, limit=50):
    """History-proof list assertion: read the section's true total from the
    API (passed in by the caller) and assert the UI renders the empty state
    IFF total == 0, otherwise exactly min(total, limit) rows. Never assumes
    the stack is freshly seeded or empty, since other agents share it.
    """
    if total == 0:
        expect(page.get_by_text(empty_text)).to_be_visible()
    else:
        rows = page.locator(f"xpath=//h3[text()='{heading_text}']/following::table[1]//tbody/tr")
        expect(rows).to_have_count(min(total, limit))


def test_local_mode_banner_and_disabled_actions(pw_page):
    """Admin sees the local-auth banner and disabled create/sync-now buttons.

    Effect-asserted against GET /auth/admin/ldap-sync/status, the same
    endpoint the page itself reads to decide the banner and gating, rather
    than trusting only the rendered text.
    """
    pw_login(pw_page)

    status = _api(pw_page, "GET", "/auth/admin/ldap-sync/status").json()
    assert status["auth_method"] == "local"

    pw_page.goto(f"{HOST_BASE_URL}/admin/ldap-sync")
    expect(pw_page.get_by_text("LDAP Directory Sync")).to_be_visible()
    expect(
        pw_page.get_by_text(
            "This deployment uses local authentication; directory sync is inactive."
        )
    ).to_be_visible()

    create_button = pw_page.get_by_role("button", name="Create mapping")
    sync_button = pw_page.get_by_role("button", name="Sync now")
    expect(create_button).to_be_disabled()
    expect(sync_button).to_be_disabled()

    # Backend-side confirmation that these actions really are refused, not
    # just visually disabled: creation requires auth_method=ldap and 409s.
    create_refused = _api(
        pw_page,
        "POST",
        "/auth/admin/ldap-sync/mappings",
        json={
            "group_dn": "cn=whatever,dc=example,dc=com",
            "herd_group_id": "00000000-0000-0000-0000-000000000000",
        },
        allow_errors=True,
    )
    assert create_refused.status_code == 409
    sync_refused = _api(pw_page, "POST", "/auth/admin/ldap-sync/run", allow_errors=True)
    assert sync_refused.status_code == 409


def test_mappings_and_runs_lists_match_the_api_totals(pw_page):
    """The mappings and runs tables render either their empty state or the
    right row count, matched against each endpoint's own `total`, not a
    hardcoded expectation of an empty stack.
    """
    pw_login(pw_page)

    mappings = _api(pw_page, "GET", "/auth/admin/ldap-sync/mappings").json()
    runs = _api(pw_page, "GET", "/auth/admin/ldap-sync/runs").json()

    pw_page.goto(f"{HOST_BASE_URL}/admin/ldap-sync")
    _assert_section_matches_total(
        pw_page, "Group mappings", "No group mappings found", mappings["total"]
    )
    _assert_section_matches_total(pw_page, "Sync runs", "No sync runs found", runs["total"])


def test_ldap_sync_page_blocked_for_non_admin(pw_page):
    """A non-admin lands back on /topology, mirroring the other admin pages' guard,
    and the backend itself refuses the same non-admin's token on every
    ldap-sync endpoint (not just the UI's client-side redirect).

    pw_page is a fresh browser context per test, so this throwaway user's
    session has no effect on any other test. The auth service exposes no
    user-delete endpoint, so the registered account cannot be torn down; it
    uses a unique timestamped username/email so it stays inert, the same
    mitigation test_acl_grants_playwright.py documents for its own throwaway
    registration.
    """
    suffix = int(time.time() * 1000)
    username = f"e2epwldap{suffix}"[:32]
    email = f"{username}@example.com"
    password = "pw-e2e-password"

    with httpx.Client(base_url=HOST_BASE_URL, verify=False, timeout=15.0) as client:
        resp = client.post(
            "/api/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        if resp.status_code not in (200, 201):
            pytest.skip(f"could not register a throwaway user: {resp.status_code} {resp.text}")

    pw_login(pw_page, email=email, password=password)
    pw_page.goto(f"{HOST_BASE_URL}/admin/ldap-sync")
    pw_page.wait_for_url("**/topology**")

    mappings = _api(pw_page, "GET", "/auth/admin/ldap-sync/mappings", allow_errors=True)
    runs = _api(pw_page, "GET", "/auth/admin/ldap-sync/runs", allow_errors=True)
    status = _api(pw_page, "GET", "/auth/admin/ldap-sync/status", allow_errors=True)
    assert mappings.status_code == 403
    assert runs.status_code == 403
    assert status.status_code == 403
