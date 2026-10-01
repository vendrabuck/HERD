"""Live proof for issue #944 (the live half of #911): a webhook receiver slower
than the consumer's ack_wait still gets the event exactly once, because the
integration consumer heartbeats `in_progress` while the fan-out runs and the
broker honors it.

The dev/test stack pins integration's NATS_ACK_WAIT_SECONDS to 4
(docker-compose.override.yml). The test registers a webhook whose target is the
integration service's own echo sink with `delay_ms=6000`, so the POST is still
unanswered 1.5x ack_wait after it arrived. It triggers one real
reservation.created through the normal producer path, waits for the ledger to
show `delivered`, and keeps watching for one more ack_wait plus the sink delay.

WHY A PEER CONSUMER: JetStream hands a timed-out message only to a pull request
that is WAITING, and a lone integration consumer fetches one message at a time
and does not fetch again until it has acked, so on its own it acks before any
redelivery could reach it (measured: with the heartbeat disabled the receiver
still got one POST). A redelivery matters when a second consumer on the durable
is waiting, which is a peer replica in production. The test plays that peer: it
binds to the same durable and starts waiting once the first POST has reached the
sink. Without the heartbeat the broker hands the peer a redelivery of the event
at ack_wait; with it, the peer sees nothing for the event.

Assertions: the sink received exactly one POST for the event (its per-event hit
counter; the ledger's unique (subscription, event) row would hide a second
POST), and the peer saw no redelivery of it. Fails fast, rather than passing
vacuously, when the stack's integration durable does not carry the short
ack_wait (a stack started before this change).
"""

import asyncio
import json
import os
import socket

import nats
import pytest
from _nats_helpers import fetch_reservation_event
from test_webhooks_flow import _poll_for_status, _register_webhook, _reservation_body

pytestmark = pytest.mark.asyncio

NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
INTEGRATION_DURABLE = "integration-webhooks-consumer"
# Must equal integration's NATS_ACK_WAIT_SECONDS in docker-compose.override.yml.
PINNED_ACK_WAIT_SECONDS = 4
# 1.5x ack_wait and safely under the 10s WEBHOOK_DELIVERY_TIMEOUT_SECONDS, so a
# second POST can only come from a broker redelivery, never a sender retry.
SINK_DELAY_MS = 6000
SINK_TARGET = f"http://integration:8000/webhooks/echo?delay_ms={SINK_DELAY_MS}"


def _nats_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=2.0):
            return True
    except OSError:
        return False


if not _nats_reachable():
    pytest.skip("NATS not reachable on localhost:4222; stack not up", allow_module_level=True)


async def _server_side_ack_wait() -> float:
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        info = await nc.jetstream().consumer_info("HERD_RESERVATIONS", INTEGRATION_DURABLE)
        return info.config.ack_wait
    finally:
        await nc.close()


async def _wait_for_peer_redelivery(event_id: str, window_seconds: float) -> list[int]:
    """Act as a second, waiting consumer on integration's durable for
    `window_seconds`; return the delivery count of each redelivery of `event_id`
    it received (empty when the broker never redelivered it).

    Any other message the peer happens to receive is returned to the stream with
    a NAK delayed past the window, so the real consumer still gets it. Our own
    redelivery is acked: it is the failure signal, and the real consumer must
    not process it a second time on top.
    """
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    redeliveries: list[int] = []
    try:
        psub = await nc.jetstream().pull_subscribe_bind(INTEGRATION_DURABLE, "HERD_RESERVATIONS")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + window_seconds
        while (remaining := deadline - loop.time()) > 0.2:
            try:
                msgs = await psub.fetch(1, timeout=remaining)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                break
            for msg in msgs:
                try:
                    ours = json.loads(msg.data).get("event_id") == event_id
                except ValueError:
                    ours = False
                if ours and msg.metadata.num_delivered > 1:
                    redeliveries.append(msg.metadata.num_delivered)
                    await msg.ack()
                else:
                    await msg.nak(delay=int(remaining) + 2)
        await psub.unsubscribe()
    finally:
        await nc.close()
    return redeliveries


async def _sink_count(admin_client, event_id: str) -> int:
    resp = await admin_client.get("/v1/webhooks/echo/hits", params={"event_id": event_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["count"]


async def test_slow_receiver_gets_the_event_exactly_once(admin_client, fresh_device):
    ack_wait = await _server_side_ack_wait()
    if ack_wait != PINNED_ACK_WAIT_SECONDS:
        pytest.fail(
            f"integration's durable has ack_wait={ack_wait!r}, expected the dev/test pin "
            f"{PINNED_ACK_WAIT_SECONDS}: recreate integration from this checkout "
            "(docker-compose.override.yml sets NATS_ACK_WAIT_SECONDS) before running this "
            "test, otherwise it would pass without proving anything"
        )

    webhook = await _register_webhook(admin_client, SINK_TARGET)
    webhook_id = webhook["id"]
    reservation_id = None
    try:
        create = await admin_client.post(
            "/v1/reservations", json=_reservation_body(fresh_device["id"])
        )
        assert create.status_code == 201, create.text
        reservation_id = create.json()["id"]

        raw_event = await fetch_reservation_event(
            reservation_id, "reservation.created", timeout=20.0
        )
        assert raw_event is not None, "reservation.created never appeared on the stream"
        event_id = json.loads(raw_event)["event_id"]

        # Wait until the first POST has ARRIVED at the sink: from here the fan-out
        # is in flight (the sink holds it for SINK_DELAY_MS) and the ack timer is
        # running. Only then does the peer start waiting, so it cannot take the
        # event's first delivery away from the real consumer.
        loop = asyncio.get_event_loop()
        arrival_deadline = loop.time() + 15.0
        while await _sink_count(admin_client, event_id) < 1:
            assert loop.time() < arrival_deadline, (
                "the sink never received the first POST for the event: another test or "
                "session sharing this stack may have queued reservation.created events "
                "ahead of it (each is held SINK_DELAY_MS by this subscription's sink)"
            )
            await asyncio.sleep(0.25)

        # A redelivery, if the heartbeat did not hold the message, comes within one
        # ack_wait of the original delivery; the extra sink delay lets a second
        # POST finish and be counted.
        window = ack_wait + SINK_DELAY_MS / 1000
        redeliveries = await _wait_for_peer_redelivery(event_id, window)

        rows = await _poll_for_status(admin_client, webhook_id, {"delivered"}, event_id=event_id)
        assert [r["status"] for r in rows] == ["delivered"], f"ledger={rows}"
        count = await _sink_count(admin_client, event_id)

        assert count == 1 and not redeliveries, (
            f"the slow sink received {count} POSTs for event {event_id} (expected exactly 1) "
            f"and a waiting peer consumer saw redeliveries {redeliveries} (expected none): "
            f"the broker redelivered the message while its fan-out was still running "
            f"(ack_wait={ack_wait}s, sink delay {SINK_DELAY_MS}ms)"
        )
    finally:
        if reservation_id:
            await admin_client.delete(f"/v1/reservations/{reservation_id}")
        await admin_client.delete(f"/v1/webhooks/{webhook_id}")
