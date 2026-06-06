"""Integration tests for ROADMAP #16 iter 2 (AI assistant tool-use loop).

Two scenarios are exercised against the live stack:

1. The execution-service auth change: non-admin callers must supply a
   reservation_id they own; unscoped /runs queries stay admin-only. No
   AI provider configuration required.

2. The full assistant loop end-to-end: stack uses the real AIClient
   against the configured provider (anthropic or openai_compat), the
   real ToolDispatcher hits the real inventory service, and the response
   shape carries the new tool_calls / tool_iterations fields. Skipped
   without an AI provider configured (no AI_API_KEY for anthropic, no
   AI_BASE_URL for openai_compat) because it makes a real model call.

The second test catches the failure modes unit tests cannot: wrong
service URLs, Traefik path-prefix mismatches, real inventory rejecting
the JWT, and the live SDK message shape diverging from our fixtures.
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from _ai_helpers import ai_provider_configured

pytestmark = pytest.mark.asyncio


# --- Execution auth change (no AI key required) ---


async def test_execution_runs_unscoped_query_rejects_non_admin(user_client):
    """GET /execution/runs without a reservation_id is admin-only.

    Iter 2 opened the route to non-admin reservation owners, but only when a
    reservation_id filter is supplied. The unscoped listing must still 403.
    """
    resp = await user_client.get("/execution/runs")
    assert resp.status_code == 403
    assert "admin" in resp.json().get("detail", "").lower()


async def test_execution_runs_with_unowned_reservation_id_rejects_non_admin(
    user_client,
):
    """A non-admin who does not own the reservation_id they pass gets 403.

    Reservations the user did not create return 404 to them, so the
    execution service's cross-service ownership check returns False and the
    auth dep raises 403.
    """
    bogus_reservation = str(uuid.uuid4())
    resp = await user_client.get(f"/execution/runs?reservation_id={bogus_reservation}")
    assert resp.status_code == 403


async def test_execution_runs_admin_still_allowed(admin_client):
    """Admin path is unchanged: list without filter still works."""
    resp = await admin_client.get("/execution/runs")
    assert resp.status_code == 200
    assert "items" in resp.json()


# --- Full assistant loop against live Anthropic + live services ---


@pytest.fixture
async def admin_reservation(admin_client, fresh_device):
    """Create a one-hour reservation on a fresh device and clean up after."""
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [fresh_device["id"]],
        "purpose": "iter2 assistant integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    create = await admin_client.post("/reservations/", json=body)
    assert create.status_code == 201, create.text
    reservation = create.json()
    try:
        yield reservation, fresh_device
    finally:
        if reservation["status"] in ("ACTIVE", "PENDING", "PENDING_PROVISION"):
            await admin_client.delete(f"/reservations/{reservation['id']}")


# Live model call: the httpx client below allows 120s. Override the global
# pytest --timeout=30 so the slow-but-legitimate assistant loop is not
# strangled; the client timeout remains the real guard.
@pytest.mark.timeout(150)
async def test_assistant_loop_response_shape_against_live_stack(
    base_url, admin_token, admin_reservation
):
    """End-to-end: hit /api/ai/reservations/{id}/assistant and assert the
    iter-2 response shape lands. Asks a question whose answer plausibly
    requires a tool call so the loop mechanics are exercised, but the
    assertion is on the response shape, not on model wording or tool count
    (the model is non-deterministic).
    """
    if not ai_provider_configured():
        pytest.skip("AI provider not configured on this host; live assistant loop not exercised")

    reservation, device = admin_reservation
    question = (
        f"What is the current configuration of device {device['id']}? "
        "If no configuration exists yet, say so."
    )

    async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
        status_resp = await client.get(f"{base_url}/ai/status")
        if not status_resp.json().get("enabled"):
            pytest.skip(
                "Host AI vars are set but /api/ai/status reports disabled; "
                "recreate the container: `docker compose up -d ai-orchestrator`."
            )

        resp = await client.post(
            f"{base_url}/ai/reservations/{reservation['id']}/assistant",
            json={"question": question},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert resp.status_code != 503, f"503 despite enabled=true: {resp.text}"
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    # Iter-2 response shape
    assert "answer" in body and isinstance(body["answer"], str) and body["answer"]
    assert "tool_calls" in body and isinstance(body["tool_calls"], list)
    assert "tool_iterations" in body and isinstance(body["tool_iterations"], int)
    assert body["tool_iterations"] >= 1
    assert body["stop_reason"]
    assert isinstance(body["input_tokens"], int) and body["input_tokens"] > 0
    assert isinstance(body["output_tokens"], int) and body["output_tokens"] > 0

    # If the model used any tools, validate each call summary's shape.
    for call in body["tool_calls"]:
        assert call["name"] in {
            "get_device",
            "get_device_ports",
            "get_device_current_config",
            "list_device_config_history",
            "get_device_config_schema",
            "find_path",
            "list_executions_for_reservation",
            "propose_config_change",
            "schedule_config_apply",
        }
        assert "arguments_summary" in call
        assert isinstance(call["duration_ms"], int)
        # error is nullable; only assert key presence
        assert "error" in call


# Live model call with a multi-step write flow: the httpx client below allows
# 180s. Override the global pytest --timeout=30 so it is not strangled; the
# client timeout remains the real guard.
@pytest.mark.timeout(210)
async def test_assistant_write_flow_discovers_schema_first(
    base_url, admin_token, admin_reservation
):
    """Branch-2 contract: when the user asks for a config change, the model
    must call get_device_config_schema before propose_config_change, then
    schedule_config_apply with dry_run=true, and the response must surface a
    non-null pending_apply for the frontend's confirmation modal.

    Skipped without an AI provider AND without write tools enabled (the latter
    is read from the test runner's env, which must match the ai-orchestrator
    container's env via .env).
    """
    import os

    if not ai_provider_configured():
        pytest.skip("AI provider not configured on this host")
    if os.environ.get("AI_WRITE_TOOLS_ENABLED", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("AI_WRITE_TOOLS_ENABLED not set on test runner; cannot assert write flow")

    reservation, device = admin_reservation
    question = (
        f"Set the hostname on device {device['id']} to demo-fw-1. "
        "Use the device's config schema to format the payload."
    )

    async with httpx.AsyncClient(verify=False, timeout=180.0) as client:
        status_resp = await client.get(f"{base_url}/ai/status")
        if not status_resp.json().get("enabled"):
            pytest.skip("ai-orchestrator reports disabled; container env may not match runner env")

        resp = await client.post(
            f"{base_url}/ai/reservations/{reservation['id']}/assistant",
            json={"question": question},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()

    tool_names = [c["name"] for c in body["tool_calls"]]
    assert "get_device_config_schema" in tool_names, (
        f"model should discover schema before proposing; saw {tool_names}"
    )
    assert "propose_config_change" in tool_names, (
        f"model should call propose_config_change for a hostname change; saw {tool_names}"
    )
    # Schema discovery must precede the first propose attempt.
    first_schema = tool_names.index("get_device_config_schema")
    first_propose = tool_names.index("propose_config_change")
    assert first_schema < first_propose, (
        f"schema discovery ({first_schema}) must come before propose_config_change "
        f"({first_propose}); tool order was {tool_names}"
    )

    # If the flow completed, schedule_config_apply must have run and the
    # confirmation modal contract must be surfaced via pending_apply.
    if "schedule_config_apply" in tool_names:
        pending = body.get("pending_apply")
        assert pending is not None, (
            "schedule_config_apply was called but pending_apply is null; "
            "frontend will not be able to mount the confirmation modal"
        )
        assert pending["dry_run"] is True, "dry_run must default to true"


async def test_assistant_reservation_404_for_non_owner(base_url, user_token):
    """Iter-1 contract preserved: non-owner gets 404 (not 403) from the assistant."""
    if not ai_provider_configured():
        pytest.skip("AI provider not configured; assistant endpoint returns 503 anyway")

    bogus = str(uuid.uuid4())
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/ai/reservations/{bogus}/assistant",
            json={"question": "any?"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404
