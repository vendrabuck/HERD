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
        # The stream is created by the reservations/execution lifespan; confirm it
        # exists rather than re-declaring it, since add_stream against a stream
        # that already exists with a different config (e.g. a configured max_age,
        # issue #620) raises instead of returning it. stream_info raises
        # nats.js.errors.NotFoundError if the stream is genuinely missing, which
        # fails this helper clean rather than silently mismatching config.
        await js.stream_info(_RESERVATIONS_STREAM)
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
        # Confirm the stream exists rather than re-declaring it (see publish_raw).
        await js.stream_info(_RESERVATIONS_STREAM)
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


async def fetch_events_for_reservation(
    reservation_id: str, subject: str, *, timeout: float = 5.0
) -> list[dict]:
    """Return every decoded payload on `subject` (HERD_RESERVATIONS) whose
    `reservation_id` field matches, in stream order.

    Deterministic alternative to a sleep-then-assert-absence guard: instead of
    settling for a window and hoping nothing landed yet, this reads the whole
    history for the subject once the outcome under test is already final, so
    the count and shape of what happened is asserted directly rather than
    inferred from timing. The consumer:

    - is EPHEMERAL (no `durable` name passed to `pull_subscribe`), so it never
      creates, competes with, or advances the services' own durable consumers
      (execution's, notifications', integration's) and needs no cleanup;
    - uses `DeliverPolicy.ALL`, so it starts at the stream's first sequence and
      sees every matching message regardless of when this consumer was created;
    - filters server-side to `subject` via the `pull_subscribe` subject arg, the
      same pattern `fetch_reservation_event` above uses;
    - stops as soon as one `fetch()` call times out (or returns nothing to
      fetch), treating that as "caught up to the head of the stream", not as an
      error.

    Only calls `stream_info`, never `add_stream`/`update_stream` (CLAUDE.md's
    #611 rule: consumers confirm a stream exists, they do not declare it).
    """
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Confirm the stream exists rather than re-declaring it (see publish_raw).
        await js.stream_info(_RESERVATIONS_STREAM)
        sub = await js.pull_subscribe(
            subject,
            stream=_RESERVATIONS_STREAM,
            config=nats.js.api.ConsumerConfig(deliver_policy=nats.js.api.DeliverPolicy.ALL),
        )
        events: list[dict] = []
        while True:
            try:
                msgs = await sub.fetch(100, timeout=timeout)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                break
            if not msgs:
                break
            for m in msgs:
                await m.ack()
                try:
                    body = json.loads(m.data)
                except Exception:  # noqa: BLE001 - skip non-JSON
                    continue
                if body.get("reservation_id") == reservation_id:
                    events.append(body)
        return events
    finally:
        await nc.close()


async def find_in_execution_dlq(marker: bytes, *, timeout: float = 15.0) -> bytes | None:
    """Poll HERD_DLQ for a message on the execution DLQ subject whose body
    contains `marker`. Returns the message bytes, or None on timeout."""
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Confirm the stream exists rather than re-declaring it (see publish_raw).
        await js.stream_info(_DLQ_STREAM)
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
