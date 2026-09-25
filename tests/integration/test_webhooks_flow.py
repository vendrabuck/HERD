"""Live integration: outbound webhook delivery end to end (issue #33, phase 4).

Requires a running stack. An admin registers a webhook subscription via
/api/v1/webhooks, a reservation is created so a reservation.created event fires
on HERD_RESERVATIONS, and the integration service's NATS consumer delivers a
signed POST to the registered target. We assert the delivery ledger:

  - a reachable 2xx receiver yields exactly one `delivered` row for the test's
    own event (idempotent; scoped by event_id, since neighboring tests' events
    also deliver to the subscription, issue #295), and
  - an unreachable / non-2xx receiver exhausts retries into a `dead` row,
    proving a failing target does not block delivery to others or the stream.

The success receiver is the integration service's own unauthenticated echo sink
(http://integration:8000/webhooks/echo), reachable on the docker network. The
service /health endpoints are GET-only and would 405 a webhook POST, so they are
not usable as a 2xx target.
"""

import asyncio
import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

import nats
import pytest
from _nats_helpers import fetch_reservation_event

pytestmark = pytest.mark.asyncio

# In-network targets (resolved by the integration container, not the host).
ECHO_TARGET = "http://integration:8000/webhooks/echo"
DEAD_TARGET = "http://reservations:8000/this-route-404s"

POLL_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0

# Issue #831: the health subscription's stream/subject, mirroring
# services/execution/app/services/health_scheduler.py's HEALTH_NATS_SUBJECT.
HEALTH_STREAM = "HERD_HEALTH"
HEALTH_NATS_SUBJECT = "herd.health.status_changed"
# herd_common is not on the integration test environment's import path (no
# tests/integration file imports it), so these mirror
# herd_common.outbox.NATS_MSG_ID_HEADER and herd_common.outbox.EVENT_ID_FIELD
# rather than importing them (same approach as test_health_alerting_flow.py).
_NATS_MSG_ID_HEADER = "Nats-Msg-Id"
_EVENT_ID_FIELD = "event_id"


def _nats_reachable() -> bool:
    """The stack maps NATS 4222 to the host; use it as a 'stack is up' probe."""
    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=2.0):
            return True
    except OSError:
        return False


if not _nats_reachable():
    pytest.skip("NATS not reachable on localhost:4222; stack not up", allow_module_level=True)


def _reservation_body(device_id: str, purpose_category: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "webhook delivery integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if purpose_category is not None:
        body["purpose_category"] = purpose_category
    return body


async def _register_webhook(
    admin_client, target_url: str, event_types: list[str] | None = None
) -> dict:
    resp = await admin_client.post(
        "/v1/webhooks",
        json={
            "target_url": target_url,
            "event_types": event_types or ["reservation.created"],
            "description": "integration webhook test",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _health_transition_payload(device_id: str) -> dict:
    """Mirrors the payload shape execution's health scheduler stages (see
    services/execution/app/services/health_scheduler.py and
    tests/integration/test_health_alerting_flow.py's _event_payload)."""
    return {
        "event": "device.health_transition",
        "event_id": str(uuid.uuid4()),
        "device_id": device_id,
        "device_name": f"webhook-health-test-{device_id[:8]}",
        "old_status": "HEALTHY",
        "new_status": "UNREACHABLE",
        "transition_kind": "bad_news",
        "consecutive_failures": 3,
        "last_run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _publish_health_event(payload: dict) -> None:
    """Publish straight to HERD_HEALTH / herd.health.status_changed, bypassing
    the outbox producer (execution's health scheduler). The caller must have
    stamped payload["event_id"]; this sets the matching Nats-Msg-Id header so
    the outbox relay's contract is mirrored exactly (issue #611): without it
    the consumer's dedupe key falls back to `<stream>:<sequence>`, and a
    container-recreated NATS (no volume) resets sequences while Postgres keeps
    prior rows under those same keys, so a rerun's insert would be swallowed
    as a redelivery. Same pattern as test_health_alerting_flow.py's
    _publish_health_event and test_failed_teardown.py's _publish_event.
    """
    nats_url = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
    nc = await nats.connect(nats_url, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Confirm the stream exists rather than re-declaring it: the stream is
        # created by execution's lifespan, and add_stream against an existing
        # stream with a different config (e.g. a configured max_age, issue
        # #620) raises instead of returning it.
        await js.stream_info(HEALTH_STREAM)
        await js.publish(
            HEALTH_NATS_SUBJECT,
            json.dumps(payload).encode(),
            headers={_NATS_MSG_ID_HEADER: payload[_EVENT_ID_FIELD]},
        )
    finally:
        await nc.close()


async def _poll_for_status(
    admin_client, webhook_id: str, statuses: set[str], event_id: str | None = None
) -> list[dict]:
    """Poll the delivery ledger until a row with one of `statuses` appears.

    When `event_id` is given, only rows for that event are considered (and
    returned): tests run against a shared stack, so a neighboring test's
    in-flight reservation event also delivers to this subscription, and an
    unscoped poll can be satisfied by that leaked row (issue #295).
    """
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        resp = await admin_client.get(f"/v1/webhooks/{webhook_id}/deliveries")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        if event_id is not None:
            rows = [r for r in rows if r["event_id"] == event_id]
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
            "/v1/reservations",
            json=_reservation_body(fresh_device["id"], purpose_category="qa_regression"),
        )
        assert create.status_code == 201, create.text
        reservation_id = create.json()["id"]
        # purpose_category (issue #646 phase 1) round-trips through the v1
        # facade create response.
        assert create.json()["purpose_category"] == "qa_regression"

        # Scope the assertion to THIS test's event. Other tests create
        # reservations against the same stack, and a neighboring
        # reservation.created event still in flight when this subscription is
        # registered is also (correctly) delivered to it, so counting all
        # delivered rows on the subscription is racy (issue #295). Read our
        # reservation's event off HERD_RESERVATIONS (matched on the unique
        # reservation_id) and pin the ledger checks to its producer-stamped
        # event_id, which is exactly the ledger row's idempotency key.
        raw_event = await fetch_reservation_event(
            reservation_id, "reservation.created", timeout=POLL_TIMEOUT_SECONDS
        )
        assert raw_event is not None, "reservation.created never appeared on the stream"
        raw_event_body = json.loads(raw_event)
        expected_event_id = raw_event_body["event_id"]
        # The outbox payload itself carries purpose_category (issue #646
        # phase 1), additive on every reservation.* lifecycle event.
        assert raw_event_body["purpose_category"] == "qa_regression"

        rows = await _poll_for_status(
            admin_client, webhook_id, {"delivered"}, event_id=expected_event_id
        )
        delivered = [r for r in rows if r["status"] == "delivered"]
        assert delivered, f"no delivered row for event {expected_event_id}; ledger={rows}"
        # Idempotent: a redelivered event must not double-send. Exactly one
        # delivered row for this event_id; do not weaken this to >= 1.
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


async def test_device_health_transition_event_type_accepted(admin_client):
    """Issue #831: the registration validator now accepts
    device.health_transition alongside the six reservation lifecycle events."""
    resp = await admin_client.post(
        "/v1/webhooks",
        json={
            "target_url": "https://example.invalid/hook",
            "event_types": ["device.health_transition"],
        },
    )
    assert resp.status_code == 201, resp.text
    webhook_id = resp.json()["id"]
    await admin_client.delete(f"/v1/webhooks/{webhook_id}")


async def test_webhook_delivered_for_health_transition(admin_client, fresh_device):
    """Issue #831: a subscription for device.health_transition receives a
    signed delivery when a health event is published on HERD_HEALTH, proving
    the integration service's second durable consumer (herd.health.*) is
    wired end to end, independently of the reservations consumer.

    The event is published directly to HERD_HEALTH rather than driven through
    the real health-polling scheduler (which needs minutes against a live
    driver to fail a device N times); the producer side is exhaustively
    covered by execution's own unit tests. This proves the integration
    service's consumer + delivery path, matching
    test_health_alerting_flow.py's approach for the notifications consumer.
    """
    webhook = await _register_webhook(
        admin_client, ECHO_TARGET, event_types=["device.health_transition"]
    )
    webhook_id = webhook["id"]
    assert webhook["secret"]

    payload = _health_transition_payload(fresh_device["id"])
    try:
        await _publish_health_event(payload)

        rows = await _poll_for_status(
            admin_client, webhook_id, {"delivered"}, event_id=payload["event_id"]
        )
        delivered = [r for r in rows if r["status"] == "delivered"]
        assert delivered, f"no delivered row for event {payload['event_id']}; ledger={rows}"
        # Idempotent, same as the reservation.created case: exactly one
        # delivered row for this event_id.
        assert len(delivered) == 1, f"expected exactly one delivered row, got {delivered}"
        assert delivered[0]["event_type"] == "device.health_transition"
        assert delivered[0]["response_status"] == 200
    finally:
        await admin_client.delete(f"/v1/webhooks/{webhook_id}")
