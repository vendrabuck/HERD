"""Integration tests for POST /api/ai/templates/suggest-identity.

Mirrors test_ai_status.py: skips when no AI provider is configured, asserts
the structured response shape when one is and the stack reports the AI as
enabled. Also covers the 503 (unconfigured) and 403 (non-admin) branches.
"""

import httpx
import pytest
from _ai_helpers import ai_provider_configured


@pytest.mark.asyncio
async def test_template_identity_503_when_unconfigured(base_url, admin_token):
    """When no AI provider is configured, the endpoint returns 503."""
    if ai_provider_configured():
        pytest.skip("AI provider is configured on this stack; 503 path is not exercised")

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/templates/suggest-identity",
            json={"name": "EX4300"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code == 503
    assert "not configured" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_template_identity_requires_admin(base_url, user_token):
    """Non-admin requests return 403 regardless of feature gate."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/templates/suggest-identity",
            json={"name": "EX4300"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 403


# Live model call: the httpx client below allows 120s. Override the global
# pytest --timeout=30 so the slow-but-legitimate call is not strangled;
# the client timeout remains the real guard.
@pytest.mark.timeout(150)
@pytest.mark.asyncio
async def test_template_identity_returns_shape_when_provider_configured(base_url, admin_token):
    """When the live AI is reachable, the response matches the documented shape."""
    if not ai_provider_configured():
        pytest.skip("AI provider not configured on this host; live AI path not exercised")

    async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
        status_resp = await client.get(f"{base_url}/ai/status")
        if not status_resp.json().get("enabled"):
            pytest.skip(
                "Host AI vars are set but /api/ai/status reports disabled. "
                "Recreate the container: `docker compose up -d ai-orchestrator`."
            )

        resp = await client.post(
            f"{base_url}/ai/templates/suggest-identity",
            json={
                "name": "EX4300",
                "description": "Juniper Networks EX Series switch",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert resp.status_code != 503, f"503 despite enabled=true: {resp.text}"
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    for key in ("vendor", "model", "part_number", "confidence", "reasoning"):
        assert key in body, f"missing {key} in response: {body}"
    assert isinstance(body["vendor"], str) and body["vendor"]
    assert isinstance(body["model"], str) and body["model"]
    assert body["confidence"] in ("low", "medium", "high")
    assert isinstance(body["reasoning"], str) and body["reasoning"]
