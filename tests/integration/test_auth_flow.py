"""Integration tests for auth flows against a running HERD stack."""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_register_login_refresh_logout(admin_client, base_url):
    """Full auth lifecycle: register, login, access protected endpoint, refresh, logout."""
    import httpx

    email = f"inttest-{uuid.uuid4().hex[:8]}@herd.example"
    username = f"inttest-{uuid.uuid4().hex[:8]}"
    password = "integration-test-password"

    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=30.0) as client:
        # Register
        reg_resp = await client.post(
            "/auth/register",
            json={"email": email, "username": username, "password": password},
        )
        assert reg_resp.status_code == 201

        # Login
        login_resp = await client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200
        tokens = login_resp.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        # Access protected endpoint
        me_resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == email

        # Refresh token
        refresh_resp = await client.post(
            "/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert refresh_resp.status_code == 200
        assert refresh_resp.json()["access_token"]
        new_refresh = refresh_resp.json()["refresh_token"]

        # Logout
        logout_resp = await client.post(
            "/auth/logout",
            json={"refresh_token": new_refresh},
        )
        assert logout_resp.status_code == 204

        # Refresh after logout should fail
        retry_resp = await client.post(
            "/auth/refresh",
            json={"refresh_token": new_refresh},
        )
        assert retry_resp.status_code == 401


async def test_role_guarded_endpoint_returns_403(user_client):
    """Regular user cannot access admin-only endpoints."""
    resp = await user_client.get("/inventory/templates")
    # Templates listing is read-only (should succeed)
    assert resp.status_code == 200

    # Try to create a template (admin-only)
    resp = await user_client.post(
        "/inventory/templates",
        json={
            "name": "Unauthorized Template",
            "sections": [
                {
                    "name": "Config",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string"},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 403


async def test_unauthenticated_returns_401(base_url):
    """No token returns 401 on protected endpoints."""
    import httpx

    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=30.0) as client:
        resp = await client.get("/auth/me")
        assert resp.status_code in (401, 403)
