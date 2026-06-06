"""Integration: LDAP auth through the running stack.

Exercises the /api/auth/login endpoint with LDAP-backed credentials,
including JIT provisioning on first login (auth_source='ldap' on the
returned /me payload).

Skipped unless:
  1. Env var HERD_INTEGRATION_LDAP=1 is set (stack is configured for LDAP).
  2. The local OpenLDAP test server is reachable on HERD_TEST_LDAP_HOST:PORT
     (defaults 127.0.0.1:389).

Expected directory layout (see services/auth/tests/test_ldap_service_live.py):
  dc=company,dc=local -> ou=people -> uid=user1..user25, password=Password1
"""

from __future__ import annotations

import os
import socket

import httpx
import pytest

pytestmark = pytest.mark.asyncio

LDAP_HOST = os.getenv("HERD_TEST_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.getenv("HERD_TEST_LDAP_PORT", "389"))
LDAP_USER = os.getenv("HERD_TEST_LDAP_USER", "user10")
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


async def test_ldap_me_reports_ldap_auth_source(base_url):
    """After login, /auth/me shows the JIT-provisioned LDAP identity."""
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
    assert user.get("auth_source") == "ldap"
    assert user.get("email") == LDAP_EXPECTED_EMAIL
    assert user.get("username") == LDAP_USER
