"""Playwright e2e test for the admin About page (issue #846 lane 3, frontend).

Follows the effect-assertion discipline used throughout tests/e2e/: the
version shown for each service row is checked against what that service's
own GET /version answers, read back directly over the API, never just what
the UI happened to render. Uses the pw_page fixture (plain playwright sync
API, per the pw_browser fixture's docstring in conftest.py) and the shared
pw_login/pw_api helpers.

Mutates nothing (only ever reads /version and /admin/about), so there is no
baseline to restore.

Depends on two sibling lanes of issue #846 that are separate parallel
branches: the backend lane's GET /version endpoint on all 12 services, and
the build-plumbing lane's Docker/Makefile wiring that supplies
VITE_HERD_BUILD/VITE_HERD_BUILD_DATE and the equivalent backend build
identifiers. Until all three lanes are merged, /admin/about renders but
every service reports "dev"/"unreachable" rather than a real build string;
this test only asserts the displayed version/build MATCH the API's own
answer, so it holds either way.

NOT RUN by this agent (no Docker stack in this worktree, and this agent must
not touch the running stack). Requires the live gate (`make test-e2e` or
`make test-e2e-seeded`) to execute.
"""

import uuid

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

# Mirrors frontend/src/api/about.ts's SERVICES constant (issue #846). Kept in
# sync by hand: one side is TypeScript, the other Python. Two irregular
# paths, per the fixed interface contract: integration answers under its
# versioned facade prefix (/api/v1/version), not its own service prefix.
SERVICES = [
    ("acl", "ACL", "/acl/version"),
    ("ai-orchestrator", "AI Orchestrator", "/ai/version"),
    ("auth", "Auth", "/auth/version"),
    ("cabling", "Cabling", "/cabling/version"),
    ("config", "Config", "/config/version"),
    ("execution", "Execution", "/execution/version"),
    ("integration", "Integration", "/v1/version"),
    ("inventory", "Inventory", "/inventory/version"),
    ("notifications", "Notifications", "/notifications/version"),
    ("reservations", "Reservations", "/reservations/version"),
    ("secrets", "Secrets", "/secrets/version"),
    ("user-profile", "User Profile", "/user-profile/version"),
]


def _row(page, label: str):
    """The About page table row whose Service cell reads exactly `label`."""
    return page.locator("table tbody tr").filter(
        has=page.get_by_role("cell", name=label, exact=True)
    )


def test_about_page_service_versions_match_the_api_readback(pw_page):
    """Every service row's displayed version equals what GET /version, read
    back directly over the API, reports for that service. A service that
    answers unreachable over the API is allowed to render "unreachable" on
    the page too, but one that answers must show its exact version string,
    never a stale or fabricated one.
    """
    pw_login(pw_page)
    pw_page.goto(f"{HOST_BASE_URL}/admin/about")
    expect(pw_page.get_by_role("heading", name="About")).to_be_visible()

    for _name, label, path in SERVICES:
        row = _row(pw_page, label)
        expect(row).to_be_visible()

        resp = pw_api(pw_page, "GET", path, allow_errors=True)
        if resp.status_code != 200:
            expect(row.get_by_text("unreachable")).to_be_visible()
            continue

        body = resp.json()
        expect(row.get_by_text("reachable")).to_be_visible()
        expect(row).to_contain_text(body["version"])


def test_non_admin_redirected_from_about_page(pw_page):
    """A non-admin visiting /admin/about is redirected like any other admin
    route (mirrors tests/e2e/test_admin_guard_redirect_playwright.py)."""
    suffix = uuid.uuid4().hex[:8]
    email = f"e2e-about-{suffix}@example.com"
    password = f"e2e-about-{suffix}-pw1!"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.post(
            f"{HOST_BASE_URL}/api/auth/register",
            json={"email": email, "username": f"e2e-about-{suffix}", "password": password},
        )
    if resp.status_code == 409:
        pytest.skip("local registration disabled (AUTH_METHOD=ldap); cannot provision a user")
    assert resp.status_code == 201, resp.text

    pw_login(pw_page, email, password)
    pw_page.goto(f"{HOST_BASE_URL}/admin/about")
    pw_page.wait_for_url("**/topology**")
    assert "/admin" not in pw_page.url
