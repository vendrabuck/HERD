"""Shared machinery for outbound dispatchers (email, chat, webhook).

Outbound channels share two concerns that the in-app channel does not:

1. Idempotency. The in-app channel dedupes on the notifications table's
   partial-unique index. Outbound sends have no such row, so this module
   provides a `reserve` / `commit` pair against the `outbound_deliveries`
   ledger. `reserve` inserts a pending ledger row inside its own transaction;
   if the unique index rejects it, the (channel, user, dedupe_key) was already
   delivered and the caller skips the side effect. The reservation is committed
   only after the side effect succeeds, so a failed send rolls back and is
   retried on the next NATS delivery.

2. Failure isolation. `run_outbound` wraps the whole attempt so a transport
   error on one channel is logged and swallowed, never blocking the other
   channels or in-app delivery (the issue's "one channel failing must not
   block others" criterion). The fail-open posture matches the rest of the
   service.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError

from app.models.outbound_delivery import OutboundDelivery
from app.services.dispatchers.base import DispatchMessage

logger = logging.getLogger(__name__)


async def already_delivered(session_factory, channel: str, message: DispatchMessage) -> bool:
    """Reserve a ledger slot. Returns True if this send was already delivered.

    A null dedupe_key is never deduplicated (not event-sourced), so we return
    False without writing a row. Otherwise we attempt to insert a pending row;
    an IntegrityError means a prior delivery already claimed the slot.
    """
    if message.dedupe_key is None:
        return False
    async with session_factory() as session:
        session.add(
            OutboundDelivery(
                channel=channel,
                user_id=message.user_id,
                event_type=message.event_type,
                dedupe_key=message.dedupe_key,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return True
    return False


async def _release(session_factory, channel: str, message: DispatchMessage) -> None:
    """Drop the ledger row for a failed send so it can be retried.

    Best-effort: a failure to clean up is logged, not raised. The worst case is
    a dropped notification on a later retry, which is preferable to crashing the
    consumer loop.
    """
    if message.dedupe_key is None:
        return
    from sqlalchemy import delete

    try:
        async with session_factory() as session:
            await session.execute(
                delete(OutboundDelivery).where(
                    OutboundDelivery.channel == channel,
                    OutboundDelivery.user_id == message.user_id,
                    OutboundDelivery.dedupe_key == message.dedupe_key,
                )
            )
            await session.commit()
    except Exception:
        logger.warning(
            "Failed to release outbound ledger row after send failure",
            extra={
                "action": "outbound_release_failed",
                "channel": channel,
                "user_id": str(message.user_id),
                "dedupe_key": message.dedupe_key,
            },
            exc_info=True,
        )


async def run_outbound(
    session_factory,
    channel: str,
    message: DispatchMessage,
    send: Callable[[], Awaitable[None]],
) -> None:
    """Dedupe, send, and isolate failures for one outbound channel.

    Order of operations:
    1. Reserve the ledger slot. If already delivered, no-op (redelivery).
    2. Run `send`. On success the slot stays claimed.
    3. On failure, release the slot so a future redelivery retries, and swallow
       the exception so other channels and in-app delivery proceed.
    """
    if await already_delivered(session_factory, channel, message):
        logger.info(
            "Suppressed duplicate outbound notification",
            extra={
                "action": "notification_deduped",
                "channel": channel,
                "user_id": str(message.user_id),
                "event_type": message.event_type,
                "dedupe_key": message.dedupe_key,
            },
        )
        return
    try:
        await send()
    except Exception:
        logger.error(
            "Outbound dispatch failed; other channels unaffected",
            extra={
                "action": "outbound_dispatch_failed",
                "channel": channel,
                "user_id": str(message.user_id),
                "event_type": message.event_type,
            },
            exc_info=True,
        )
        await _release(session_factory, channel, message)
        return
    logger.info(
        "Delivered outbound notification",
        extra={
            "action": "notification_delivered",
            "channel": channel,
            "user_id": str(message.user_id),
            "event_type": message.event_type,
        },
    )
