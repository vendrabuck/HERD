"""NATS consumer: fan reservation lifecycle and device health events out to
registered webhooks.

Two durable pull consumers, mirroring the notifications service's shape
(services/notifications/app/services/nats_consumer.py):

- HERD_RESERVATIONS / herd.reservations.* (reservation lifecycle events)
- HERD_HEALTH / herd.health.* (issue #831: device.health_transition)

Both route through the same `handle_event`, since every payload on both
streams carries the same `event` discriminator key (reservation payloads use
it, e.g. "reservation.created"; execution's health scheduler stamps
"device.health_transition" under the identical key, see
services/execution/app/services/health_scheduler.py); `handle_event` needs no
per-stream branching to read it. Distinct durable consumer names and DLQ
subjects, so a stuck health-event subscriber cannot block reservation
delivery and vice versa. `load_matching_targets` fans a message out to every
active WebhookSubscription whose `event_types` lists that event name
(app.services.delivery). The delivery ledger is the durable record: a slow or
failing receiver lands a `dead` ledger row, it never NAKs the NATS message, so
one bad target cannot re-fan-out to the others or stall the stream. The
message is NAK'd / dead-lettered only on an undecodable payload or an
unexpected consumer-loop error.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from herd_common.jetstream import (
    ensure_consumer,
    ensure_stream_exists,
    nak_delay,
    parse_nak_backoff_schedule,
)
from herd_common.outbox import event_dedupe_key

from app.config import settings
from app.services.delivery import deliver_one, load_matching_targets

logger = logging.getLogger(__name__)

NATS_STREAM = "HERD_RESERVATIONS"
NATS_SUBJECT_PATTERN = "herd.reservations.*"
NATS_DURABLE = "integration-webhooks-consumer"
# JetStream consumer policy (issue #895: no `backoff` on ConsumerConfig; a
# `backoff` list used to make JetStream silently replace the server-side
# ack_wait with backoff[0], measured 1s against nats-server 2.10.29 instead of
# the intended 30s). Redelivery timing for a transient error comes entirely
# from the explicit `nak(delay=...)` call in process_message's transient
# branch (NATS_NAK_BACKOFF_SECONDS below), not from this config.
NATS_MAX_DELIVER = 5
NATS_ACK_WAIT_SECONDS = 30
# NAK-delay schedule (issue #895), from Settings so it is a knob
# (NATS_NAK_BACKOFF_SECONDS): production defaults to [1, 5, 15, 60, 120];
# docker-compose.override.yml pins a short dev/test schedule. Parsed once at
# import time; nak_delay() maps a message's num_delivered to the entry to
# pass as msg.nak(delay=...).
NATS_NAK_BACKOFF_SECONDS = parse_nak_backoff_schedule(settings.nats_nak_backoff_seconds)
# 4-token DLQ subject, deliberately outside the 3-token consumer filter so a
# dead-lettered message is not re-consumed in a poison loop.
NATS_DLQ_SUBJECT = "herd.reservations.dlq.integration"
NATS_FETCH_TIMEOUT_SECONDS = 5

# Issue #831: second durable consumer for device.health_transition events on
# HERD_HEALTH. Own stream, subject filter, durable name, and DLQ subject; same
# max_deliver/ack_wait/fetch-timeout knobs (no backoff, issue #895) and the
# same batch == 1 rule (issue #648) as the reservations consumer.
HEALTH_STREAM = "HERD_HEALTH"
HEALTH_SUBJECT_PATTERN = "herd.health.*"
HEALTH_DURABLE = "integration-webhooks-health-consumer"
HEALTH_DLQ_SUBJECT = "herd.health.dlq.integration"


async def handle_event(
    event_data: dict,
    raw_body: bytes,
    session_factory: Callable,
    dedupe_key: str | None,
) -> None:
    """Fan one reservation or health event out to every matching active
    subscription.

    `dedupe_key` is the stable payload event_id used as the per-subscription
    delivery idempotency key. Delivery failures are recorded in the ledger, not
    raised, so the NATS message is acked regardless of individual outcomes.
    """
    event_name = event_data.get("event")
    if not event_name:
        logger.debug("Event missing `event` field; nothing to deliver")
        return
    targets = await load_matching_targets(session_factory, event_name)
    if not targets:
        return
    results = await asyncio.gather(
        *[
            deliver_one(
                session_factory,
                target,
                raw_body,
                str(dedupe_key),
                event_name,
                timeout=settings.webhook_delivery_timeout_seconds,
                attempts=settings.webhook_delivery_attempts,
            )
            for target in targets
        ],
        return_exceptions=True,
    )
    for target, result in zip(targets, results):
        if isinstance(result, Exception):
            # An unexpected error (e.g. the DB was unreachable) while recording a
            # delivery. We still ack the message; re-fanning out to every target
            # because one ledger write failed would double-send to the rest.
            logger.error(
                "Unexpected error delivering to webhook %s",
                target.id,
                exc_info=result,
            )


async def _publish_to_dlq(js, payload: bytes, subject: str = NATS_DLQ_SUBJECT) -> None:
    try:
        await js.publish(subject, payload)
    except Exception:
        logger.error("Failed to publish to integration webhooks DLQ", exc_info=True)


async def process_message(
    msg,
    js,
    handler: Callable[[dict, bytes, Callable, str | None], Awaitable[None]],
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
            extra={"action": "nats_poison_message", "dlq_subject": dlq_subject},
        )
        await _publish_to_dlq(js, msg.data, subject=dlq_subject)
        await msg.ack()
        return "dlq"

    try:
        await handler(
            event_data,
            msg.data,
            session_factory,
            event_dedupe_key(event_data, msg),
        )
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


async def start_nats_consumer(app) -> None:
    """Start both webhook delivery consumers as background tasks during lifespan.

    Two subscriptions, set up identically apart from their stream/subject/
    durable/DLQ knobs (see the module docstring): HERD_RESERVATIONS /
    herd.reservations.* (the original consumer) and HERD_HEALTH /
    herd.health.* (issue #831). Both dispatch to the same `handle_event`.
    """
    import nats
    from nats.js.api import ConsumerConfig

    try:
        nc = await nats.connect(
            settings.nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        app.state.nats = nc
        js = nc.jetstream()

        from app.database import AsyncSessionLocal

        async def _make_subscription(
            stream: str,
            subject_pattern: str,
            durable: str,
            dlq_subject: str,
        ):
            try:
                await ensure_stream_exists(js, name=stream, subjects=[subject_pattern])
            except Exception:
                logger.warning("Could not create/update NATS stream %s", stream, exc_info=True)

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
                        # No messages this cycle; the fetch also re-establishes
                        # delivery after a broker reconnect.
                        continue
                    except Exception:
                        logger.warning(
                            "NATS pull fetch failed for %s; will retry",
                            subject_pattern,
                            exc_info=True,
                        )
                        await asyncio.sleep(NATS_FETCH_TIMEOUT_SECONDS)
                        continue
                    for msg in msgs:
                        try:
                            await process_message(
                                msg,
                                js,
                                handle_event,
                                AsyncSessionLocal,
                                dlq_subject=dlq_subject,
                            )
                        except Exception:
                            logger.error(
                                "Unexpected error in webhook consumer loop for %s",
                                subject_pattern,
                                exc_info=True,
                            )

            return asyncio.create_task(_consumer_loop())

        app.state.nats_consumer_task = await _make_subscription(
            NATS_STREAM, NATS_SUBJECT_PATTERN, NATS_DURABLE, NATS_DLQ_SUBJECT
        )
        app.state.nats_health_consumer_task = await _make_subscription(
            HEALTH_STREAM, HEALTH_SUBJECT_PATTERN, HEALTH_DURABLE, HEALTH_DLQ_SUBJECT
        )
        logger.info("Integration webhook NATS consumers started (reservations + health)")

    except Exception:
        logger.warning(
            "Failed to connect to NATS; integration webhooks will not be delivered",
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
