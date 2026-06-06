"""Integration coverage for the AI orchestrator's feature-gate endpoints.

These run against the live stack and confirm that /api/ai/status reports the
feature state without requiring an AI provider configuration, and that
/api/ai/generate returns 503 when no provider is configured (or a valid
non-503 when one is).

Honors the deprecation fallback: AI_API_KEY is the canonical credential;
ANTHROPIC_API_KEY is honored for one release.
"""

import httpx
import pytest
from _ai_helpers import ai_provider_configured


@pytest.mark.asyncio
async def test_ai_status_returns_enabled_flag(base_url):
    """/api/ai/status is reachable without auth and returns enabled + provider + model."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{base_url}/ai/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert isinstance(body["enabled"], bool)
    assert body.get("provider") in ("anthropic", "openai_compat")
    assert isinstance(body.get("model"), str) and body["model"]


@pytest.mark.asyncio
async def test_ai_status_matches_env_config(base_url):
    """The enabled flag mirrors the orchestrator's ai_is_configured() resolution."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{base_url}/ai/status")
    assert resp.json()["enabled"] == ai_provider_configured()


@pytest.mark.asyncio
async def test_ai_generate_503_when_unconfigured(base_url, user_token):
    """When no provider is configured, /api/ai/generate gates behind a 503."""
    if ai_provider_configured():
        pytest.skip("AI provider is configured on this stack; 503 path is not exercised")

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/generate",
            data={"prompt": "anything"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 503
    assert "not configured" in resp.json().get("detail", "").lower()


# Live model call: the httpx client below allows 120s. Override the global
# pytest --timeout=30 so the slow-but-legitimate generate is not strangled;
# the client timeout remains the real guard.
@pytest.mark.timeout(150)
@pytest.mark.asyncio
async def test_ai_generate_succeeds_when_provider_configured(base_url, admin_token):
    """When the AI provider is configured, exercise the live AI path end-to-end.

    Uses the admin token so inventory visibility is unrestricted. Accepts a
    502/409 whose detail mentions "available" as a valid outcome: integration
    tests do not require `seed_devices.py` to have been run, so the stack may
    have zero DUTs of the templates the AI proposes; reaching that error still
    proves the credential flowed through to the AI provider.

    The generator constrains template_name to a live-inventory enum and repairs
    one bad proposal via a re-prompt (see services/ai-orchestrator generator.py
    and ai_client.py), so an "unknown templates" 502 is a real regression here
    and is NOT tolerated.
    """
    if not ai_provider_configured():
        pytest.skip(
            "AI provider not configured on this host (set AI_API_KEY for anthropic, "
            "or AI_BASE_URL for openai_compat); live AI path not exercised"
        )

    async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
        status_resp = await client.get(f"{base_url}/ai/status")
        if not status_resp.json().get("enabled"):
            pytest.skip(
                "Host AI vars are set but /api/ai/status reports disabled. "
                "The ai-orchestrator container reads env at compose-up time; "
                "recreate it: `docker compose up -d ai-orchestrator`."
            )

        resp = await client.post(
            f"{base_url}/ai/generate",
            data={"prompt": "Two switches connected to one server"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert resp.status_code != 503, f"503 despite enabled=true: {resp.text}"

    if resp.status_code == 502 and "available" in resp.text:
        return

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    for key in ("purpose", "devices", "edges"):
        assert key in body, f"missing {key} in response: {body}"
    assert isinstance(body["devices"], list)
    assert isinstance(body["edges"], list)
