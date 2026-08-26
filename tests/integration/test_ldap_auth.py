"""Integration: LDAP auth through the running stack.

Exercises the /api/auth/login endpoint with LDAP-backed credentials,
including JIT provisioning on first login. /auth/me's UserResponse schema
(services/auth/app/schemas/auth.py) does not expose auth_source at all (a
deliberate absence: adding it would be an API contract change, out of scope
here), so the JIT-provisioned identity's auth_source is confirmed by reading
auth.users back directly in Postgres instead (see
test_ldap_login_provisions_ldap_user).

Skipped unless:
  1. Env var HERD_INTEGRATION_LDAP=1 is set (stack is configured for LDAP).
  2. The local OpenLDAP test server is reachable on HERD_TEST_LDAP_HOST:PORT
     (defaults 127.0.0.1:389).

Expected directory layout (see services/auth/tests/test_ldap_service_live.py):
  dc=company,dc=local -> ou=people -> uid=user1..user25, password=Password1

Default login identity is ldapit-eng1 (infra/ldap-test/ldif/70-seed-integration.ldif),
not one of the userN fixtures: a gate stack seeded via `make seed`
(seed_devices_public.py) already holds LOCAL users user1..user1000 in the same
users table (auth_source="local"), so JIT-provisioning uid=userN over LDAP
would collide on username (auth_service.py's username_collision path) on any
seeded stack. The ldapit-* uids can never match the seed script's
`user[0-9]+` / `admin[0-9]+` patterns, so this file's login coverage works
whether or not the stack has been seeded.

Runs in the Makefile's _gate-ldap-stack-tests phase (master, everything, and
nightly), which switches the ephemeral gate stack's auth service to LDAP mode
after e2e and restores it to local mode afterward, then in
tests/integration/test_ldap_sync_admin.py (the sync-admin-surface companion
to this file's login coverage); before that phase existed the stack always
booted AUTH_METHOD=local, so this whole file self-skipped everywhere,
including in every gate (issue #572).

Cleanup: every test here logs in as LDAP_USER (ldapit-eng1 by default),
JIT-provisioning its auth.users row; a module-scoped autouse fixture deletes
every ldapit-% row afterward so the stack's baseline state is unchanged by a
run of this file, matching test_ldap_sync_admin.py's own teardown.
"""

from __future__ import annotations

import os
import socket

import httpx
import pytest
from conftest import _psql

pytestmark = pytest.mark.asyncio

LDAP_HOST = os.getenv("HERD_TEST_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.getenv("HERD_TEST_LDAP_PORT", "389"))
LDAP_USER = os.getenv("HERD_TEST_LDAP_USER", "ldapit-eng1")
LDAP_PASSWORD = os.getenv("HERD_TEST_LDAP_PASSWORD", "Password1")
LDAP_EXPECTED_EMAIL = os.getenv(
    "HERD_TEST_LDAP_EMAIL", f"{LDAP_USER}@company.local"
)


def _ldap_reachable() -> bool:
    try:
        with socket.create_connection((LDAP_HOST, LDAP_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("HERD_INTEGRATION_LDAP") != "1",
        reason="HERD_INTEGRATION_LDAP not set; stack is not configured for LDAP auth.",
    ),
    pytest.mark.skipif(
        not _ldap_reachable(),
        reason=f"No LDAP server reachable at ldap://{LDAP_HOST}:{LDAP_PORT}",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _cleanup_ldapit_rows():
    """Delete every ldapit-% auth.users row this module's tests JIT-provision.

    Autouse so it runs even though no individual test requests it; module
    scope so it fires once, after every test here has finished, not between
    tests. A skipped test never instantiates this fixture at all (the skip
    marks above are checked before fixture setup), so a HERD_INTEGRATION_LDAP-
    unset run touches Postgres exactly as much as it touches LDAP: not at all.
    """
    yield
    _psql("DELETE FROM auth.users WHERE username LIKE 'ldapit-%'")


async def test_ldap_user_login_succeeds(base_url):
    """An LDAP user can obtain a JWT via /auth/login."""
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.post(
            f"{base_url}/auth/login",
            json={"email": LDAP_USER, "password": LDAP_PASSWORD},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body


async def test_ldap_user_wrong_password_returns_401(base_url):
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.post(
            f"{base_url}/auth/login",
            json={"email": LDAP_USER, "password": "definitely-not-correct"},
        )
    assert resp.status_code == 401


async def test_ldap_unknown_user_returns_401(base_url):
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.post(
            f"{base_url}/auth/login",
            json={"email": "does-not-exist-in-directory", "password": LDAP_PASSWORD},
        )
    assert resp.status_code == 401


async def test_ldap_login_provisions_ldap_user(base_url):
    """After login, /auth/me confirms the JIT-provisioned identity's email
    and username, and a direct Postgres read-back confirms auth_source='ldap'
    (see the module docstring for why /auth/me itself is not the source for
    that field).
    """
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        login = await client.post(
            f"{base_url}/auth/login",
            json={"email": LDAP_USER, "password": LDAP_PASSWORD},
        )
        login.raise_for_status()
        token = login.json()["access_token"]

        me = await client.get(
            f"{base_url}/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
    assert me.status_code == 200
    user = me.json()
    assert user.get("email") == LDAP_EXPECTED_EMAIL
    assert user.get("username") == LDAP_USER

    result = _psql(
        f"SELECT auth_source FROM auth.users WHERE username='{LDAP_USER}'",
        tuples_only=True,
    )
    assert result.returncode == 0, f"auth_source read-back failed: {result.stdout} {result.stderr}"
    assert result.stdout.strip() == "ldap", result.stdout
