"""NATS consumer: subscribe to reservation lifecycle events and dispatch notifications."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from app.config import settings
from app.services import event_router
from app.services.dispatchers import InAppDispatcher
from app.services.dispatchers.base import Dispatcher, DispatchMessage
from app.services.preferences_client import get_preferences_client

logger = logging.getLogger(__name__)

NATS_STREAM = "HERD_RESERVATIONS"
NATS_SUBJECT_PATTERN = "herd.reservations.*"
NATS_DURABLE = "notifications-consumer"
NATS_MAX_DELIVER = 5
NATS_ACK_WAIT_SECONDS = 30
NATS_BACKOFF_SECONDS = [1, 5, 15, 60, 120]
NATS_DLQ_SUBJECT = "herd.reservations.dlq.notifications"

# ROADMAP #13 iter 2: second subscription for device health transitions.
# Same handler dispatch (event_router branches by `event` field) but a
# distinct durable consumer + DLQ so a stuck health-event consumer cannot
# stall the reservation event stream and vice versa.
HEALTH_STREAM = "HERD_HEALTH"
HEALTH_SUBJECT_PATTERN = "herd.health.*"
HEALTH_DURABLE = "notifications-health-consumer"
HEALTH_DLQ_SUBJECT = "herd.health.dlq.notifications"


def _dedupe_key_from_msg(msg) -> str | None:
    """Build a stable per-message dedupe key from JetStream metadata.

    Uses "<stream>:<stream-sequence>", which is identical across redeliveries of
    the same message and distinct for every new publish. Returns None when the
    metadata is unavailable (e.g. a non-JetStream test stub), so dedup is simply
    skipped rather than failing the message.
    """
    meta = getattr(msg, "metadata", None)
    seq = getattr(meta, "sequence", None)
    stream_seq = getattr(seq, "stream", None)
    stream = getattr(meta, "stream", None)
    if stream_seq is None:
        return None
    return f"{stream or 'stream'}:{stream_seq}"


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
    dispatchers = dispatchers if dispatchers is not None else [InAppDispatcher()]
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
    """Process one NATS message. Returns 'ack', 'nak', or 'dlq'."""
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
        await handler(event_data, session_factory, _dedupe_key_from_msg(msg))
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
        nc = await nats.connect(settings.nats_url)
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
                await js.add_stream(name=stream, subjects=[subject_pattern])
            except Exception:
                logger.warning(
                    "Could not create/update NATS stream %s",
                    stream,
                    exc_info=True,
                )

            sub = await js.subscribe(
                subject_pattern,
                durable=durable,
                manual_ack=True,
                config=ConsumerConfig(
                    max_deliver=NATS_MAX_DELIVER,
                    ack_wait=NATS_ACK_WAIT_SECONDS,
                    backoff=NATS_BACKOFF_SECONDS,
                ),
            )

            async def _consumer_loop():
                async for msg in sub.messages:
                    try:
                        await process_message(
                            msg,
                            js,
                            handle_event,
                            _get_db_session,
                            dlq_subject=dlq_subject,
                        )
                    except Exception:
                        logger.error(
                            "Unexpected error in NATS consumer loop for %s",
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
