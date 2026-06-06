"""Integration test for Branch 3: multi-turn conversation round-trip.

Verifies the API contract end-to-end against the live stack: first turn
returns a fresh conversation_id; second turn supplied with that id is
accepted and echoes the same id back; a second turn supplied with a bogus
id returns 404. Light on model assertions because model wording is
non-deterministic; the contract assertions are what protect us against
regressions in the persistence layer wiring.

Skipped without an AI provider configured (returns 503 otherwise).
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from _ai_helpers import ai_provider_configured

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def admin_reservation(admin_client, fresh_device):
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [fresh_device["id"]],
        "purpose": "branch-3 multi-turn integration",
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


# Live model call across two turns: the httpx client below allows 120s.
# Override the global pytest --timeout=30 so it is not strangled; the client
# timeout remains the real guard.
@pytest.mark.timeout(150)
async def test_first_turn_returns_conversation_id_and_second_turn_echoes_it(
    base_url, admin_token, admin_reservation
):
    if not ai_provider_configured():
        pytest.skip("AI provider not configured; assistant endpoint returns 503")

    reservation, _device = admin_reservation
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
        status_resp = await client.get(f"{base_url}/ai/status")
        if not status_resp.json().get("enabled"):
            pytest.skip("/api/ai/status reports disabled")

        first = await client.post(
            f"{base_url}/ai/reservations/{reservation['id']}/assistant",
            json={"question": "What devices are in this reservation?"},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        conv_id = first_body["conversation_id"]
        assert conv_id, "first turn must return a non-null conversation_id"
        uuid.UUID(conv_id)

        second = await client.post(
            f"{base_url}/ai/reservations/{reservation['id']}/assistant",
            json={
                "question": "What was the first device's name?",
                "conversation_id": conv_id,
            },
            headers=headers,
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["conversation_id"] == conv_id, (
            "second turn must echo the same conversation_id"
        )


async def test_unknown_conversation_id_returns_404(base_url, admin_token, admin_reservation):
    if not ai_provider_configured():
        pytest.skip("AI provider not configured")

    reservation, _device = admin_reservation
    bogus = str(uuid.uuid4())

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/ai/reservations/{reservation['id']}/assistant",
            json={"question": "anything?", "conversation_id": bogus},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code == 404
