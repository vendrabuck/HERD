"""Playwright e2e: AdminGuard redirects a non-admin off every guarded route.

Issue #575 item 2. `tests/e2e/test_register_and_roles.py` (Selenium) already
covers the live redirect for 2 of the 15 AdminGuard-guarded paths
(`/admin/users`, `/reporting`); `frontend/src/test/routes.test.tsx` pins the
full 15-path set structurally (route-table membership, no render). This
module covers the live redirect behavior for the remaining 13 paths in one
Playwright test, logging in once as a seeded non-admin and iterating them,
since a fresh module-scoped Selenium browser per path would be 13x the
browser-startup cost for the same assertion the existing pattern already
makes once.

The two `:id` paths (`/admin/groups/:id`, `/admin/device-groups/:id`) are
visited with a syntactically-plausible placeholder id: AdminGuard sits above
route param resolution and any data fetch (frontend/src/components/guards.tsx
useEffect fires on mount before the page component does anything with the
param), so the redirect fires before an invalid id could matter.

Self-provisions its non-admin account via POST /api/auth/register rather
than using a seed_devices_public.py account: in the make everything gate the
stack is seeded only AFTER the test phases (the seed exists to leave a demo
behind), so during e2e no seeded users exist and tests must create what they
need (the standing self-seed convention; this file learned that the hard way
on 2026-08-24). The account is marker-named like the Selenium suite's
e2e-roles accounts and persists, since auth exposes no user-delete endpoint
(see test_register_and_roles.py's module docstring for that precedent). A
409 from /auth/register means AUTH_METHOD=ldap (local registration disabled;
the uuid suffix makes a name collision impossible), mirroring the Selenium
suite's LDAP note, so the test skips there.
"""

import uuid

import httpx
import pytest

from .conftest import HOST_BASE_URL, pw_login

# The 15 AdminGuard-guarded paths pinned in frontend/src/test/routes.test.tsx's
# EXPECTED_ADMIN_GUARDED_PATHS, minus the 2 already covered live by the
# Selenium test_register_and_roles.py (/admin/users, /reporting). The two
# :id placeholders below stand in for /admin/groups/:id and
# /admin/device-groups/:id.
REMAINING_GUARDED_PATHS = [
    "/admin/add-device",
    "/admin/groups",
    "/admin/groups/new",
    "/admin/groups/00000000-0000-0000-0000-000000000000",
    "/admin/device-groups",
    "/admin/device-groups/new",
    "/admin/device-groups/00000000-0000-0000-0000-000000000000",
    "/admin/connections",
    "/admin/drivers",
    "/admin/grants",
    "/admin/hypervisors",
    "/admin/ldap-sync",
    "/admin/purpose-review",
]


def _register_non_admin() -> tuple[str, str]:
    """Create a fresh role="user" account through the public register API and
    return its (email, password). Skips the test in LDAP mode, where local
    registration is disabled by design."""
    suffix = uuid.uuid4().hex[:8]
    email = f"e2e-adminguard-{suffix}@example.com"
    password = f"e2e-adminguard-{suffix}-pw1!"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.post(
            f"{HOST_BASE_URL}/api/auth/register",
            json={"email": email, "username": f"e2e-adminguard-{suffix}", "password": password},
        )
    if resp.status_code == 409:
        pytest.skip("local registration disabled (AUTH_METHOD=ldap); cannot provision a user")
    assert resp.status_code == 201, resp.text
    return email, password


def test_non_admin_redirected_from_remaining_admin_guarded_paths(pw_page):
    """A role="user" account visiting any of the 13 remaining guarded paths
    lands on /topology, never on the admin path it requested."""
    email, password = _register_non_admin()
    pw_login(pw_page, email, password)

    for path in REMAINING_GUARDED_PATHS:
        pw_page.goto(f"{HOST_BASE_URL}{path}")
        pw_page.wait_for_url("**/topology**")
        assert "/admin" not in pw_page.url, f"{path} did not redirect away from /admin"
        assert "/reporting" not in pw_page.url, f"{path} did not redirect away"
