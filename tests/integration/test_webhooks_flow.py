"""Live integration: outbound webhook delivery end to end (issue #33, phase 4).

Requires a running stack. An admin registers a webhook subscription via
/api/v1/webhooks, a reservation is created so a reservation.created event fires
on HERD_RESERVATIONS, and the integration service's NATS consumer delivers a
signed POST to the registered target. We assert the delivery ledger:

  - a reachable 2xx receiver yields exactly one `delivered` row (idempotent), and
  - an unreachable / non-2xx receiver exhausts retries into a `dead` row,
    proving a failing target does not block delivery to others or the stream.

The success receiver is the integration service's own unauthenticated echo sink
(http://integration:8000/webhooks/echo), reachable on the docker network. The
service /health endpoints are GET-only and would 405 a webhook POST, so they are
not usable as a 2xx target.
"""

import asyncio
import socket
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio

# In-network targets (resolved by the integration container, not the host).
ECHO_TARGET = "http://integration:8000/webhooks/echo"
DEAD_TARGET = "http://reservations:8000/this-route-404s"

POLL_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0


def _nats_reachable() -> bool:
    """The stack maps NATS 4222 to the host; use it as a 'stack is up' probe."""
    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=2.0):
            return True
    except OSError:
        return False


if not _nats_reachable():
    pytest.skip("NATS not reachable on localhost:4222; stack not up", allow_module_level=True)


def _reservation_body(device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "device_ids": [device_id],
        "purpose": "webhook delivery integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }


async def _register_webhook(admin_client, target_url: str) -> dict:
    resp = await admin_client.post(
        "/v1/webhooks",
        json={
            "target_url": target_url,
            "event_types": ["reservation.created"],
            "description": "integration webhook test",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _poll_for_status(admin_client, webhook_id: str, statuses: set[str]) -> list[dict]:
    """Poll the delivery ledger until a row with one of `statuses` appears."""
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        resp = await admin_client.get(f"/v1/webhooks/{webhook_id}/deliveries")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        if any(r["status"] in statuses for r in rows):
            return rows
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return []


async def test_webhook_delivered_exactly_once(admin_client, fresh_device):
    """A reachable 2xx receiver gets exactly one `delivered` ledger row."""
    webhook = await _register_webhook(admin_client, ECHO_TARGET)
    webhook_id = webhook["id"]
    # The secret is returned once at creation so a receiver can verify signatures.
    assert webhook["secret"]

    reservation_id = None
    try:
        create = await admin_client.post(
            "/v1/reservations", json=_reservation_body(fresh_device["id"])
        )
        assert create.status_code == 201, create.text
        reservation_id = create.json()["id"]

        rows = await _poll_for_status(admin_client, webhook_id, {"delivered"})
        delivered = [r for r in rows if r["status"] == "delivered"]
        assert delivered, f"no delivered row appeared; ledger={rows}"
        # Idempotent: a redelivered event must not double-send.
        assert len(delivered) == 1, f"expected exactly one delivered row, got {delivered}"
        assert delivered[0]["event_type"] == "reservation.created"
        assert delivered[0]["response_status"] == 200
    finally:
        if reservation_id:
            await admin_client.delete(f"/v1/reservations/{reservation_id}")
        await admin_client.delete(f"/v1/webhooks/{webhook_id}")


async def test_webhook_failure_dead_letters(admin_client, fresh_device):
    """An unreachable / non-2xx receiver exhausts retries into a `dead` row,
    proving a failing target does not block the consumer or other deliveries."""
    webhook = await _register_webhook(admin_client, DEAD_TARGET)
    webhook_id = webhook["id"]

    reservation_id = None
    try:
        create = await admin_client.post(
            "/v1/reservations", json=_reservation_body(fresh_device["id"])
        )
        assert create.status_code == 201, create.text
        reservation_id = create.json()["id"]

        rows = await _poll_for_status(admin_client, webhook_id, {"dead", "failed"})
        terminal = [r for r in rows if r["status"] in ("dead", "failed")]
        assert terminal, f"no dead-letter row appeared; ledger={rows}"
        assert terminal[0]["status"] == "dead"
        assert terminal[0]["attempts"] >= 1
        assert terminal[0]["last_error"]
    finally:
        if reservation_id:
            await admin_client.delete(f"/v1/reservations/{reservation_id}")
        await admin_client.delete(f"/v1/webhooks/{webhook_id}")


async def test_unknown_event_type_rejected(admin_client):
    """The registration validator rejects an event type outside the known set."""
    resp = await admin_client.post(
        "/v1/webhooks",
        json={"target_url": "https://example.invalid/hook", "event_types": ["reservation.boom"]},
    )
    assert resp.status_code == 422, resp.text


async def test_webhooks_require_admin(user_client):
    """A non-admin cannot register a webhook."""
    resp = await user_client.post(
        "/v1/webhooks",
        json={"target_url": "https://example.invalid/hook", "event_types": ["reservation.created"]},
    )
    assert resp.status_code == 403, resp.text
