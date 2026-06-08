"""ROADMAP #18 Pluggable LLM Provider: cross-service wiring coverage.

The existing `test_ai_status.py` covers the high-level enabled flag and the
503 path for /api/ai/generate. This file rounds out the integration coverage
of the new config surface itself:

- /api/ai/status is unauthenticated and shape-stable, including when an
  invalid AI_PROVIDER would be set (we cannot mutate the env at runtime,
  so we assert the contract is enforced; the value is always one of the
  known providers when reported as enabled).
- The three guarded routes (/api/ai/generate, /api/ai/templates/suggest-identity,
  /api/ai/reservations/{id}/assistant) all return 401 before they evaluate
  ai_is_configured(). Auth-vs-gate ordering matters; an unauthenticated 503
  would leak feature state.
- The status payload's `model` and `provider` values reflect what the
  orchestrator was started with. This is a drift detector for the case
  where someone edits .env without recreating the ai-orchestrator container.
- When AI_BASE_URL targets a local Anthropic-compatible endpoint (e.g. vLLM)
  with AI_API_KEY blank, the orchestrator still reports enabled=true. We cannot
  mutate the running container's env, so we run this assertion conditionally:
  only when the env truly uses the keyless-local path on this host. On a host
  configured with a hosted-API key, the test skips with an informative message.

All tests run against the live stack at https://localhost; tests that need
the live AI follow the same gating idiom as test_ai_assistant_tools.py.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from _ai_helpers import ai_provider_configured

pytestmark = pytest.mark.asyncio


# --- /api/ai/status: unauthenticated contract ---


async def test_ai_status_is_unauthenticated(base_url):
    """The status endpoint must not require a bearer token; the frontend
    config-gate calls it before login is enabled."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{base_url}/ai/status")
    assert resp.status_code == 200, resp.text


async def test_ai_status_provider_value_is_known(base_url):
    """The reported provider is one of the two supported names; tooling
    elsewhere (frontend, ops docs) keys off these strings."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        body = (await client.get(f"{base_url}/ai/status")).json()
    assert body["provider"] in ("anthropic", "openai_compat"), (
        f"unknown provider name in status payload: {body['provider']!r}"
    )


async def test_ai_status_payload_shape_is_stable(base_url):
    """Shape contract: status returns exactly the documented keys; the
    frontend treats this payload as a public schema."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        body = (await client.get(f"{base_url}/ai/status")).json()
    assert set(body.keys()) == {"enabled", "provider", "model"}, (
        f"unexpected keys in status: {sorted(body.keys())}"
    )


# --- Drift detector: env-vs-runtime ---


async def test_ai_status_provider_matches_env(base_url):
    """Compares the stack's reported provider against the .env value.

    Drift here means the ai-orchestrator container has stale env (it reads
    AI_PROVIDER at compose-up time, not on /status request). This is the
    integration-tier check for a known footgun: `docker compose restart
    ai-orchestrator` does NOT re-read .env; `docker compose up -d
    ai-orchestrator` does.
    """
    expected = (os.getenv("AI_PROVIDER", "").strip() or "anthropic").lower()
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        body = (await client.get(f"{base_url}/ai/status")).json()
    assert body["provider"] == expected, (
        f"provider drift: .env says {expected!r}, /api/ai/status says {body['provider']!r}. "
        "Recreate the ai-orchestrator container: docker compose up -d ai-orchestrator"
    )


async def test_ai_status_model_matches_env(base_url):
    """Same drift detector as above, for AI_MODEL."""
    expected_model = os.getenv("AI_MODEL", "").strip()
    if not expected_model:
        pytest.skip("AI_MODEL not set in env; cannot compare against status payload")
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        body = (await client.get(f"{base_url}/ai/status")).json()
    assert body["model"] == expected_model, (
        f"model drift: .env says {expected_model!r}, /api/ai/status says {body['model']!r}. "
        "Recreate the ai-orchestrator container: docker compose up -d ai-orchestrator"
    )


# --- Auth-vs-gate ordering on the three guarded routes ---
#
# These tests assert that 401 (missing bearer) is returned BEFORE the
# ai_is_configured() check fires. If a future refactor moves the
# unconfigured 503 ahead of the auth dependency, an unauthenticated probe
# could fingerprint whether a deployment has an AI key configured.


async def test_generate_requires_auth_before_gate(base_url):
    """No bearer token: must be 401, not 503, regardless of AI config."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(f"{base_url}/ai/generate", data={"prompt": "x"})
    assert resp.status_code in (401, 403), (
        f"expected 401/403 without auth, got {resp.status_code}: {resp.text}"
    )


async def test_suggest_identity_requires_auth_before_gate(base_url):
    """No bearer token: must be 401, not 503, regardless of AI config."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/templates/suggest-identity",
            json={"name": "EX4300"},
        )
    assert resp.status_code in (401, 403), (
        f"expected 401/403 without auth, got {resp.status_code}: {resp.text}"
    )


async def test_assistant_requires_auth_before_gate(base_url):
    """No bearer token on the assistant route: 401, not 503."""
    bogus_reservation = str(uuid.uuid4())
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/reservations/{bogus_reservation}/assistant",
            json={"question": "hi"},
        )
    assert resp.status_code in (401, 403), (
        f"expected 401/403 without auth, got {resp.status_code}: {resp.text}"
    )


# --- Three-route 503 gating when unconfigured ---
#
# test_ai_status.py covers /generate. test_template_identity.py covers
# /templates/suggest-identity. Add the assistant 503 path here so all
# three guarded routes have an explicit 503 case from the integration tier.


async def test_assistant_returns_503_when_provider_not_configured(base_url, user_token):
    """The assistant route gates on ai_is_configured() and returns 503
    before evaluating the reservation_id (so the gate cannot be used to
    enumerate reservation existence)."""
    if ai_provider_configured():
        pytest.skip("AI provider is configured on this stack; 503 path is not exercised")

    bogus_reservation = str(uuid.uuid4())
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/reservations/{bogus_reservation}/assistant",
            json={"question": "anything"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 503, f"expected 503 when unconfigured, got {resp.status_code}"
    assert "not configured" in resp.json().get("detail", "").lower()


# --- Keyless anthropic against a local endpoint ---


async def test_anthropic_enabled_with_base_url_and_no_key(base_url):
    """Anthropic is configured when AI_BASE_URL targets a local
    Anthropic-compatible endpoint (e.g. vLLM), even with AI_API_KEY blank.

    The .env on this stack may supply a key (hosted API) instead; in that case
    the keyless-local path is not exercised and we skip with a clear message.
    """
    provider = (os.getenv("AI_PROVIDER", "").strip() or "anthropic").lower()
    if provider != "anthropic":
        pytest.skip("Stack is not in anthropic mode; keyless-local only applies there")

    key = os.getenv("AI_API_KEY", "").strip()
    base = os.getenv("AI_BASE_URL", "").strip()
    if key:
        pytest.skip(
            "AI_API_KEY is set on this host; the keyless-local path is not "
            "exercised. Clear AI_API_KEY (leaving AI_BASE_URL set) and recreate "
            "the ai-orchestrator container to exercise this branch."
        )
    if not base:
        pytest.skip("Neither AI_API_KEY nor AI_BASE_URL is set; nothing to validate")

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        body = (await client.get(f"{base_url}/ai/status")).json()
    assert body["enabled"] is True, (
        "AI_BASE_URL is set with no key but /api/ai/status reports disabled. "
        "Anthropic against a local endpoint should be enabled on base_url alone."
    )
    assert body["provider"] == "anthropic"
