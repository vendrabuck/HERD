"""Direct-NATS helpers for integration tests that exercise redelivery and DLQ
behavior against a running stack.

NATS is reached from the test host (NATS_URL_HOST, default localhost:4222),
mirroring test_dlq_and_idempotency.py and test_health_alerting_flow.py; callers
should probe reachability and skip when the host cannot reach NATS. Reads are
non-destructive: an ephemeral pull consumer over a limits-retention stream
leaves the messages in place.
"""

import asyncio
import json
import os

import nats

NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")

_RESERVATIONS_STREAM = "HERD_RESERVATIONS"
_RESERVATIONS_SUBJECTS = ["herd.reservations.*"]
_DLQ_STREAM = "HERD_DLQ"
_DLQ_SUBJECTS = ["herd.*.dlq.>"]
_EXECUTION_DLQ_SUBJECT = "herd.reservations.dlq.execution"


async def probe_nats() -> str | None:
    """Return None when NATS is reachable from the host, else the error text."""
    try:
        nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
        await nc.close()
        return None
    except Exception as exc:  # noqa: BLE001 - host may not reach NATS in some envs
        return str(exc)


async def publish_raw(subject: str, payload: bytes) -> None:
    """Publish raw bytes to the reservations stream (bypassing the producer).

    No Nats-Msg-Id header is set, so JetStream assigns a new sequence rather
    than deduping: exactly what a relay republish after the dedupe window looks
    like to the consumer.
    """
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Idempotent: the stream is created by the reservations/execution lifespan.
        await js.add_stream(name=_RESERVATIONS_STREAM, subjects=_RESERVATIONS_SUBJECTS)
        await js.publish(subject, payload)
    finally:
        await nc.close()


async def fetch_reservation_event(
    reservation_id: str, event: str, *, timeout: float = 30.0
) -> bytes | None:
    """Return the raw bytes of the `event` message for `reservation_id` from
    HERD_RESERVATIONS, or None on timeout."""
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        await js.add_stream(name=_RESERVATIONS_STREAM, subjects=_RESERVATIONS_SUBJECTS)
        sub = await js.pull_subscribe("herd.reservations.*", stream=_RESERVATIONS_STREAM)
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await sub.fetch(100, timeout=2)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                msgs = []
            for m in msgs:
                await m.ack()
                try:
                    body = json.loads(m.data)
                except Exception:  # noqa: BLE001 - skip non-JSON
                    continue
                if body.get("event") == event and body.get("reservation_id") == reservation_id:
                    return m.data
            await asyncio.sleep(0.3)
        return None
    finally:
        await nc.close()


async def find_in_execution_dlq(marker: bytes, *, timeout: float = 15.0) -> bytes | None:
    """Poll HERD_DLQ for a message on the execution DLQ subject whose body
    contains `marker`. Returns the message bytes, or None on timeout."""
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        await js.add_stream(name=_DLQ_STREAM, subjects=_DLQ_SUBJECTS)  # idempotent
        sub = await js.pull_subscribe(_EXECUTION_DLQ_SUBJECT, stream=_DLQ_STREAM)
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await sub.fetch(100, timeout=2)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                msgs = []
            for m in msgs:
                await m.ack()
                if marker in m.data:
                    return m.data
            await asyncio.sleep(0.3)
        return None
    finally:
        await nc.close()
