"""Playwright e2e: AdminGuard redirects a non-admin off every guarded route.

Issue #575 item 2. `tests/e2e/test_register_and_roles.py` (Selenium) already
covers the live redirect for 2 of the 14 AdminGuard-guarded paths
(`/admin/users`, `/reporting`); `frontend/src/test/routes.test.tsx` pins the
full 14-path set structurally (route-table membership, no render). This
module covers the live redirect behavior for the remaining 12 paths in one
Playwright test, logging in once as a seeded non-admin and iterating them,
since a fresh module-scoped Selenium browser per path would be 12x the
browser-startup cost for the same assertion the existing pattern already
makes once.

The two `:id` paths (`/admin/groups/:id`, `/admin/device-groups/:id`) are
visited with a syntactically-plausible placeholder id: AdminGuard sits above
route param resolution and any data fetch (frontend/src/components/guards.tsx
useEffect fires on mount before the page component does anything with the
param), so the redirect fires before an invalid id could matter.

Uses the same seeded non-admin credentials as tests/load/locustfile.py
(`user{i}@herd.dev` / `user{i}user{i}xx`, from seed_devices_public.py's
generate_users) rather than registering a throwaway account: cheaper, and
this stack is always seeded before e2e runs. Nothing is mutated, so there is
no baseline to restore.
"""

from .conftest import HOST_BASE_URL, pw_login

NON_ADMIN_EMAIL = "user1@herd.dev"
NON_ADMIN_PASSWORD = "user1user1xx"

# The 14 AdminGuard-guarded paths pinned in frontend/src/test/routes.test.tsx's
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
]


def test_non_admin_redirected_from_remaining_admin_guarded_paths(pw_page):
    """A role="user" account visiting any of the 12 remaining guarded paths
    lands on /topology, never on the admin path it requested."""
    pw_login(pw_page, NON_ADMIN_EMAIL, NON_ADMIN_PASSWORD)

    for path in REMAINING_GUARDED_PATHS:
        pw_page.goto(f"{HOST_BASE_URL}{path}")
        pw_page.wait_for_url("**/topology**")
        assert "/admin" not in pw_page.url, f"{path} did not redirect away from /admin"
        assert "/reporting" not in pw_page.url, f"{path} did not redirect away"
