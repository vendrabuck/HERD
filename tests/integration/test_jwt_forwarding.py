"""Negative tests for inter-service auth: JWT forwarding and internal-token paths.

The HERD stack uses two distinct service-to-service auth modes:

1. JWT forwarding (the default): caller forwards the user's Bearer token
   downstream so the target service enforces RBAC as the real user.
2. Internal token (legacy/scoped): a small set of endpoints under `/internal`
   are guarded by an `X-Internal-Token` header instead of a JWT, for cases
   where the caller acts on behalf of the system rather than a real user.

This file proves both gates reject everything they should and that there is
no accidental fallback between schemes (no JWT-only access to internal
endpoints; no internal-token-only access to JWT-guarded endpoints).

Endpoints under test:

- `/inventory/devices/{id}` (JWT-guarded; called by reservations + execution)
- `/inventory/devices/{id}/internal` (X-Internal-Token guarded; the matching
  inter-service variant of the same data path)
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from jose import jwt

pytestmark = pytest.mark.asyncio


def _mint_jwt(payload_overrides: dict | None = None) -> str:
    """Mint a JWT with the stack's real secret. Used for forged/expired tokens."""
    secret = os.getenv("AUTH_SECRET_KEY")
    algorithm = os.getenv("AUTH_ALGORITHM", "HS256")
    assert secret, "AUTH_SECRET_KEY missing from environment; integration conftest should load .env"

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "username": "phantom",
        "role": "user",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return jwt.encode(payload, secret, algorithm=algorithm)


# A random UUID is fine for every test below. Auth always fires before the DB
# lookup, so the negative cases never reach the not-found branch; the positive
# control deliberately expects 404 (proves the request got past auth).
_FAKE_DEVICE = str(uuid.uuid4())


# --- JWT-guarded endpoint: /inventory/devices/{id} ---


async def test_jwt_endpoint_rejects_missing_auth(base_url):
    """No Authorization header on a JWT path is rejected before handler logic."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{base_url}/inventory/devices/{_FAKE_DEVICE}")
    assert resp.status_code in (401, 403), resp.text


async def test_jwt_endpoint_rejects_malformed_bearer(base_url):
    """Random bytes in place of a JWT must fail signature verification (401)."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}",
            headers={"Authorization": "Bearer not.a.real.jwt"},
        )
    assert resp.status_code == 401, resp.text


async def test_jwt_endpoint_rejects_expired_token(base_url):
    """A JWT signed with the right key but with `exp` in the past is 401."""
    expired = _mint_jwt({"exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())})
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert resp.status_code == 401, resp.text


async def test_jwt_endpoint_does_not_accept_internal_token_as_fallback(base_url):
    """The internal token must not unlock JWT-guarded endpoints."""
    internal = os.getenv("INTERNAL_API_TOKEN")
    assert internal, "INTERNAL_API_TOKEN missing from environment"

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}",
            headers={"X-Internal-Token": internal},
        )
    assert resp.status_code in (401, 403), resp.text


async def test_jwt_endpoint_accepts_valid_admin_token(base_url, admin_token):
    """Positive control: a real admin JWT lands past auth (404 for fake device)."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code == 404, resp.text


# --- Internal-token-guarded endpoint: /inventory/devices/{id}/internal ---


async def test_internal_endpoint_rejects_missing_token(base_url):
    """No X-Internal-Token must fail. FastAPI surfaces missing required header as 422."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{base_url}/inventory/devices/{_FAKE_DEVICE}/internal")
    assert resp.status_code in (403, 422), resp.text


async def test_internal_endpoint_rejects_wrong_token(base_url):
    """Wrong X-Internal-Token must fail with 403 from `_require_internal_token`."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}/internal",
            headers={"X-Internal-Token": "definitely-not-the-real-token"},
        )
    assert resp.status_code == 403, resp.text


async def test_internal_endpoint_does_not_accept_jwt_as_fallback(base_url, admin_token):
    """A valid admin JWT must not unlock internal-token endpoints."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}/internal",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code in (403, 422), resp.text


async def test_internal_endpoint_accepts_valid_internal_token(base_url):
    """Positive control: the right internal token lands past auth (404 for fake device)."""
    internal = os.getenv("INTERNAL_API_TOKEN")
    assert internal, "INTERNAL_API_TOKEN missing from environment"

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(
            f"{base_url}/inventory/devices/{_FAKE_DEVICE}/internal",
            headers={"X-Internal-Token": internal},
        )
    assert resp.status_code == 404, resp.text
