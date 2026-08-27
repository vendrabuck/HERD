"""NATS consumer: fan reservation lifecycle events out to registered webhooks.

A durable pull consumer on HERD_RESERVATIONS (subject herd.reservations.*) loads
every active subscription matching the event and POSTs a signed copy to each,
concurrently. The delivery ledger (app.services.delivery) is the durable record:
a slow or failing receiver lands a `dead` ledger row, it never NAKs the NATS
message, so one bad target cannot re-fan-out to the others or stall the stream.
The message is NAK'd / dead-lettered only on an undecodable payload or an
unexpected consumer-loop error, mirroring the notifications consumer.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from herd_common.jetstream import ensure_stream_exists
from herd_common.outbox import event_dedupe_key

from app.config import settings
from app.services.delivery import deliver_one, load_matching_targets

logger = logging.getLogger(__name__)

NATS_STREAM = "HERD_RESERVATIONS"
NATS_SUBJECT_PATTERN = "herd.reservations.*"
NATS_DURABLE = "integration-webhooks-consumer"
NATS_MAX_DELIVER = 5
NATS_ACK_WAIT_SECONDS = 30
NATS_BACKOFF_SECONDS = [1, 5, 15, 60, 120]
# 4-token DLQ subject, deliberately outside the 3-token consumer filter so a
# dead-lettered message is not re-consumed in a poison loop.
NATS_DLQ_SUBJECT = "herd.reservations.dlq.integration"
NATS_FETCH_BATCH = 10
NATS_FETCH_TIMEOUT_SECONDS = 5


async def handle_event(
    event_data: dict,
    raw_body: bytes,
    session_factory: Callable,
    dedupe_key: str | None,
) -> None:
    """Fan one reservation event out to every matching active subscription.

    `dedupe_key` is the stable payload event_id used as the per-subscription
    delivery idempotency key. Delivery failures are recorded in the ledger, not
    raised, so the NATS message is acked regardless of individual outcomes.
    """
    event_name = event_data.get("event")
    if not event_name:
        logger.debug("Reservation event missing `event` field; nothing to deliver")
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
    """Process one NATS message. Returns 'ack', 'nak', or 'dlq'."""
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
        logger.warning(
            "Transient error processing NATS message; NAK for retry",
            extra={
                "action": "nats_message_nak",
                "delivered": num_delivered,
                "event": event_data.get("event"),
            },
            exc_info=exc,
        )
        await msg.nak()
        return "nak"

    await msg.ack()
    return "ack"


async def start_nats_consumer(app) -> None:
    """Start the webhook delivery consumer as a background task during lifespan."""
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

        try:
            await ensure_stream_exists(js, name=NATS_STREAM, subjects=[NATS_SUBJECT_PATTERN])
        except Exception:
            logger.warning("Could not create/update NATS stream %s", NATS_STREAM, exc_info=True)

        psub = await js.pull_subscribe(
            NATS_SUBJECT_PATTERN,
            durable=NATS_DURABLE,
            config=ConsumerConfig(
                max_deliver=NATS_MAX_DELIVER,
                ack_wait=NATS_ACK_WAIT_SECONDS,
                backoff=NATS_BACKOFF_SECONDS,
            ),
        )

        async def _consumer_loop():
            while True:
                try:
                    msgs = await psub.fetch(NATS_FETCH_BATCH, timeout=NATS_FETCH_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    raise
                except (nats.errors.TimeoutError, asyncio.TimeoutError):
                    # No messages this cycle; the fetch also re-establishes
                    # delivery after a broker reconnect.
                    continue
                except Exception:
                    logger.warning(
                        "NATS pull fetch failed for %s; will retry",
                        NATS_SUBJECT_PATTERN,
                        exc_info=True,
                    )
                    await asyncio.sleep(NATS_FETCH_TIMEOUT_SECONDS)
                    continue
                for msg in msgs:
                    try:
                        await process_message(msg, js, handle_event, AsyncSessionLocal)
                    except Exception:
                        logger.error(
                            "Unexpected error in webhook consumer loop",
                            exc_info=True,
                        )

        app.state.nats_consumer_task = asyncio.create_task(_consumer_loop())
        logger.info("Integration webhook NATS consumer started")

    except Exception:
        logger.warning(
            "Failed to connect to NATS; integration webhooks will not be delivered",
            exc_info=True,
        )


async def stop_nats_consumer(app) -> None:
    task = getattr(app.state, "nats_consumer_task", None)
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
