"""NATS consumer: subscribe to reservation lifecycle events and dispatch notifications."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from herd_common.jetstream import (
    connect_nats,
    ensure_consumer,
    ensure_stream_exists,
    heartbeat_interval,
    nak_delay,
    parse_nak_backoff_schedule,
    process_batch_with_heartbeat,
)
from herd_common.outbox import event_dedupe_key

from app.config import settings
from app.services import event_router
from app.services.dispatchers import default_dispatchers
from app.services.dispatchers.base import Dispatcher, DispatchMessage
from app.services.preferences_client import get_preferences_client

logger = logging.getLogger(__name__)

NATS_STREAM = "HERD_RESERVATIONS"
NATS_SUBJECT_PATTERN = "herd.reservations.*"
NATS_DURABLE = "notifications-consumer"
# JetStream consumer policy (issue #895: no `backoff` on ConsumerConfig; a
# `backoff` list used to make JetStream silently replace the server-side
# ack_wait with backoff[0], measured 1s against nats-server 2.10.29 instead of
# the intended 30s). Redelivery timing for a transient error comes entirely
# from the explicit `nak(delay=...)` call in process_message's transient
# branch (NATS_NAK_BACKOFF_SECONDS below), not from this config.
NATS_MAX_DELIVER = 5
# Read from settings (NATS_ACK_WAIT_SECONDS, issue #944; production 30, validated
# at least 2 at load). Never hardcode it here.
NATS_ACK_WAIT_SECONDS = settings.nats_ack_wait_seconds
# Work-in-progress heartbeat cadence (issue #911, the shared
# herd_common.jetstream.keep_messages_alive, which execution's loop shares too,
# issue #944).
# The loop resets the ack timer on this interval so a slow dispatcher (email,
# outbound channels) cannot trigger an ack-timeout redelivery of a message
# still in flight.
# Half of ack_wait leaves margin for a late heartbeat; a crashed consumer stops
# heartbeating, so ack_wait still expires and the message correctly redelivers.
NATS_HEARTBEAT_SECONDS = heartbeat_interval(NATS_ACK_WAIT_SECONDS)
# NAK-delay schedule (issue #895), from Settings so it is a knob
# (NATS_NAK_BACKOFF_SECONDS): production defaults to [1, 5, 15, 60, 120];
# docker-compose.override.yml pins a short dev/test schedule. Parsed once at
# import time; nak_delay() maps a message's num_delivered to the entry to
# pass as msg.nak(delay=...).
NATS_NAK_BACKOFF_SECONDS = parse_nak_backoff_schedule(settings.nats_nak_backoff_seconds)
NATS_DLQ_SUBJECT = "herd.reservations.dlq.notifications"
# Pull-consumer fetch tuning. A pull consumer re-establishes on the next fetch
# after a broker reconnect, which a push subscription does not do reliably, so it
# survives a NATS restart (issue #21).
NATS_FETCH_TIMEOUT_SECONDS = 5

# ROADMAP #13 iter 2: second subscription for device health transitions.
# Same handler dispatch (event_router branches by `event` field) but a
# distinct durable consumer + DLQ so a stuck health-event consumer cannot
# stall the reservation event stream and vice versa.
HEALTH_STREAM = "HERD_HEALTH"
HEALTH_SUBJECT_PATTERN = "herd.health.*"
HEALTH_DURABLE = "notifications-health-consumer"
HEALTH_DLQ_SUBJECT = "herd.health.dlq.notifications"


async def _dispatch(
    dispatchers: list[Dispatcher],
    session_factory,
    message: DispatchMessage,
) -> None:
    prefs = await get_preferences_client().get(message.user_id)
    if not prefs.event_enabled(message.event_type):
        logger.debug(
            "Event opted out by user prefs",
            extra={
                "action": "notification_opted_out",
                "event": message.event_type,
                "user_id": str(message.user_id),
            },
        )
        return
    for dispatcher in dispatchers:
        if not prefs.channel_enabled(dispatcher.channel):
            continue
        await dispatcher.send(session_factory, message)


async def handle_event(
    event: dict,
    session_factory,
    dedupe_key: str | None = None,
    dispatchers: list[Dispatcher] | None = None,
) -> None:
    dispatchers = dispatchers if dispatchers is not None else default_dispatchers()
    messages = await event_router.build_messages(event)
    for message in messages:
        # Stamp the source-message dedupe key on every recipient row so a
        # redelivery collapses per (user, message). Uniqueness is the composite
        # (user_id, dedupe_key), so distinct recipients each still get a row.
        message.dedupe_key = dedupe_key
        await _dispatch(dispatchers, session_factory, message)


async def _publish_to_dlq(js, payload: bytes, subject: str = NATS_DLQ_SUBJECT) -> None:
    try:
        await js.publish(subject, payload)
    except Exception:
        logger.error("Failed to publish to notifications DLQ", exc_info=True)


async def process_message(
    msg,
    js,
    handler: Callable[[dict, Callable, str | None], Awaitable[None]],
    session_factory: Callable,
    *,
    max_deliver: int = NATS_MAX_DELIVER,
    dlq_subject: str = NATS_DLQ_SUBJECT,
) -> str:
    """Process one NATS message. Returns 'ack', 'nak', or 'dlq'.

    A transient failure NAKs with an explicit `delay=` (issue #895: a bare
    `msg.nak()` redelivers immediately regardless of any ConsumerConfig
    `backoff`, which itself only ever timed ack-timeout redeliveries, never a
    NAK), so the redelivery actually waits
    NATS_NAK_BACKOFF_SECONDS[min(num_delivered - 1, ...)] seconds.
    """
    try:
        event_data = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.error(
            "Poison message on stream; routing to DLQ",
            extra={
                "action": "nats_poison_message",
                "size": len(msg.data),
                "dlq_subject": dlq_subject,
            },
        )
        await _publish_to_dlq(js, msg.data, subject=dlq_subject)
        await msg.ack()
        return "dlq"

    try:
        # Idempotency key: the producer-stamped payload `event_id` (outbox,
        # issue #21), which survives a relay republish under a new stream
        # sequence; falls back to "<stream>:<sequence>" for pre-outbox events.
        await handler(event_data, session_factory, event_dedupe_key(event_data, msg))
    except Exception as exc:
        num_delivered = getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
        if num_delivered >= max_deliver:
            logger.error(
                "Message exhausted max_deliver; routing to DLQ",
                extra={
                    "action": "nats_dlq_exhausted",
                    "delivered": num_delivered,
                    "event": event_data.get("event"),
                    "dlq_subject": dlq_subject,
                },
                exc_info=exc,
            )
            await _publish_to_dlq(js, msg.data, subject=dlq_subject)
            await msg.ack()
            return "dlq"
        delay = nak_delay(num_delivered, NATS_NAK_BACKOFF_SECONDS)
        logger.warning(
            "Transient error processing NATS message; NAK for retry",
            extra={
                "action": "nats_message_nak",
                "delivered": num_delivered,
                "delay_seconds": delay,
                "event": event_data.get("event"),
            },
            exc_info=exc,
        )
        await msg.nak(delay=delay)
        return "nak"

    await msg.ack()
    return "ack"


async def process_batch(
    msgs: list,
    js,
    session_factory: Callable,
    *,
    dlq_subject: str = NATS_DLQ_SUBJECT,
    handler: Callable[[dict, Callable, str | None], Awaitable[None]] | None = None,
) -> None:
    """Process one fetched batch with the in-progress heartbeat running (issue
    #911): every message not yet settled, the running one and any queued
    behind it, gets `in_progress` every NATS_HEARTBEAT_SECONDS, so a slow
    handler never reaches ack_wait. Every durable's consumer loop goes through
    here, so no loop can skip the heartbeat."""

    # Resolved at call time so a test patching `handle_event` still takes effect.
    handler_fn = handler or handle_event

    async def _one(msg) -> None:
        await process_message(msg, js, handler_fn, session_factory, dlq_subject=dlq_subject)

    await process_batch_with_heartbeat(msgs, _one, NATS_HEARTBEAT_SECONDS)


async def start_nats_consumer(app) -> None:
    """Start the NATS consumers as background tasks during app lifespan.

    Two subscriptions:
    - HERD_RESERVATIONS / herd.reservations.* (since iter 1 of notifications)
    - HERD_HEALTH / herd.health.* (ROADMAP #13 iter 2)

    Both route through the same `handle_event` since `event_router` branches
    by the `event` field. Distinct durable consumer names + DLQs so a stuck
    health-event subscriber cannot block reservation events and vice versa.
    """
    import nats
    from nats.js.api import ConsumerConfig

    try:
        # Bounded first connect, unlimited reconnects once connected (issue
        # #1083): a broker that is down at boot raises after a few tries, so the
        # warning below is reachable and the service runs without the consumer;
        # an established connection still retries forever.
        nc = await connect_nats(settings.nats_url)
        app.state.nats = nc
        js = nc.jetstream()

        from app.database import AsyncSessionLocal

        def _get_db_session():
            class _SessionCtx:
                async def __aenter__(self):
                    self._session = AsyncSessionLocal()
                    return self._session

                async def __aexit__(self, *args):
                    await self._session.close()

            return _SessionCtx()

        async def _make_subscription(
            stream: str,
            subject_pattern: str,
            durable: str,
            dlq_subject: str,
        ):
            try:
                await ensure_stream_exists(js, name=stream, subjects=[subject_pattern])
            except Exception:
                logger.warning(
                    "Could not create/update NATS stream %s",
                    stream,
                    exc_info=True,
                )

            consumer_config = ConsumerConfig(
                max_deliver=NATS_MAX_DELIVER,
                ack_wait=NATS_ACK_WAIT_SECONDS,
            )
            # Create-or-update the durable to match consumer_config BEFORE
            # pull_subscribe binds to it (issue #895): pull_subscribe alone
            # only creates a durable that is missing, and otherwise binds to
            # whatever config the server already has, so a durable created
            # under the old `backoff`-carrying config would silently keep it
            # forever on a persistent NATS volume (`make prod`) without this
            # explicit update. See herd_common.jetstream.ensure_consumer.
            await ensure_consumer(
                js,
                stream=stream,
                subject=subject_pattern,
                durable=durable,
                config=consumer_config,
            )
            psub = await js.pull_subscribe(
                subject_pattern,
                durable=durable,
                config=consumer_config,
            )

            async def _consumer_loop():
                while True:
                    try:
                        # batch is deliberately 1: nats-py's multi-message fetch holds
                        # already-received messages until the batch fills or the
                        # deadline expires, which added up to NATS_FETCH_TIMEOUT_SECONDS
                        # of latency to every event (issue #648); the batch=1 path
                        # returns the first message immediately. Do not "optimize"
                        # this back to a batch without a pull API that returns
                        # partial batches promptly.
                        msgs = await psub.fetch(1, timeout=NATS_FETCH_TIMEOUT_SECONDS)
                    except asyncio.CancelledError:
                        raise
                    except (nats.errors.TimeoutError, asyncio.TimeoutError):
                        # No messages this cycle; fetch again. The fetch also
                        # re-establishes delivery after a broker reconnect.
                        continue
                    except Exception:
                        # Connection lost or reconnecting; pause then re-fetch.
                        logger.warning(
                            "NATS pull fetch failed for %s; will retry",
                            subject_pattern,
                            exc_info=True,
                        )
                        await asyncio.sleep(NATS_FETCH_TIMEOUT_SECONDS)
                        continue
                    await process_batch(
                        msgs,
                        js,
                        _get_db_session,
                        dlq_subject=dlq_subject,
                    )

            return asyncio.create_task(_consumer_loop())

        app.state.nats_consumer_task = await _make_subscription(
            NATS_STREAM, NATS_SUBJECT_PATTERN, NATS_DURABLE, NATS_DLQ_SUBJECT
        )
        app.state.nats_health_consumer_task = await _make_subscription(
            HEALTH_STREAM, HEALTH_SUBJECT_PATTERN, HEALTH_DURABLE, HEALTH_DLQ_SUBJECT
        )
        logger.info("NATS consumers started (reservations + health)")

    except Exception:
        logger.warning(
            "Failed to connect to NATS; operating without event-driven notifications",
            exc_info=True,
        )


async def stop_nats_consumer(app) -> None:
    for task_attr in ("nats_consumer_task", "nats_health_consumer_task"):
        task = getattr(app.state, task_attr, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    nc = getattr(app.state, "nats", None)
    if nc:
        try:
            await nc.close()
        except Exception:
            logger.warning("Failed to close NATS connection", exc_info=True)
