"""Transactional outbox: durable, at-least-once event delivery (issue #21).

The dual-write problem: a service that commits a state change to its database
and then publishes a NATS event in a separate step can lose the event if the
process dies or NATS is unreachable in the window between the two. A reservation
can go ACTIVE while the devices are never provisioned, with no record that an
event was owed. The outbox closes that gap: the event row is written in the SAME
transaction as the state change, so the event exists if and only if the state
transition committed. A background relay then publishes unpublished rows and
marks them sent, retrying across a messaging outage.

Schema-per-service is preserved: there is no shared outbox table. Each producing
service defines its own concrete model from `OutboxMixin` against its own Base
(so the table lives in that service's schema) and adds its own Alembic revision.
This module owns only the shared column shape, the enqueue helper, the relay
loop, the prune, and the consumer-side dedupe-key resolver.

Producer:

    from herd_common.outbox import OutboxMixin, enqueue_event, run_outbox_relay

    class OutboxEvent(OutboxMixin, Base):
        __tablename__ = "outbox"

    # in the same transaction as the state change:
    enqueue_event(session, OutboxEvent, "herd.reservations.created", payload)
    await session.commit()

Consumer (at-least-once, so dedupe on the stable id):

    from herd_common.outbox import event_dedupe_key
    key = event_dedupe_key(event_data, msg)
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Uuid, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

logger = logging.getLogger(__name__)

# Header JetStream uses for publisher-side message dedup. The relay sets it to
# the outbox row id, so a republish within the stream's dedup window is collapsed
# by the server. Consumer-side dedup on the payload event_id (see
# `event_dedupe_key`) covers a republish that lands outside that window.
NATS_MSG_ID_HEADER = "Nats-Msg-Id"

# Payload key the producer stamps with the outbox row id and the consumer keys
# its idempotency on.
EVENT_ID_FIELD = "event_id"

# JSONB on Postgres (queryable, compact), plain JSON on the SQLite unit-test
# backend.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class OutboxMixin:
    """Column shape for a per-service outbox table.

    Mixed into a concrete model in the owning service, against that service's own
    Base so the table lands in the service schema:

        class OutboxEvent(OutboxMixin, Base):
            __tablename__ = "outbox"
    """

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Indexed so the relay's `published_at IS NULL` claim and the prune's
    # `published_at < cutoff` scan stay cheap as the table grows.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


def enqueue_event(
    session: AsyncSession,
    model: type,
    subject: str,
    payload: dict[str, Any],
    *,
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Stage an event for at-least-once delivery in the caller's transaction.

    Adds an outbox row to `session` but does NOT commit: the caller commits it in
    the same transaction as the state change it describes, so the event exists
    iff that transition did. The id is injected into the payload under
    `event_id` so consumers can dedupe on a stable key that survives a relay
    republish under a new stream sequence. Returns the event id.
    """
    eid = event_id or uuid.uuid4()
    body = {**payload, EVENT_ID_FIELD: str(eid)}
    session.add(model(id=eid, subject=subject, payload=body))
    return eid


async def _publish_pending(
    session: AsyncSession,
    js: Any,
    model: type,
    *,
    batch_size: int,
) -> int:
    """Publish up to `batch_size` unpublished rows, marking each sent.

    Claims one row at a time with FOR UPDATE SKIP LOCKED so multiple relay
    instances never publish the same row, holding the row lock only across that
    row's single publish. A publish failure records the attempt, leaves the row
    unpublished for a later tick, and stops the batch (NATS is likely down, so
    there is no value hammering the rest this tick). Returns the count published.
    """
    published = 0
    for _ in range(batch_size):
        stmt = (
            select(model)
            .where(model.published_at.is_(None))
            .order_by(model.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            break
        row.attempts += 1
        try:
            await js.publish(
                row.subject,
                json.dumps(row.payload, default=str).encode(),
                headers={NATS_MSG_ID_HEADER: str(row.id)},
            )
        except Exception:
            logger.exception("outbox publish failed for event %s; will retry", row.id)
            # Persist the attempt bump; the row stays unpublished for a later tick.
            await session.commit()
            break
        row.published_at = datetime.now(timezone.utc)
        await session.commit()
        published += 1
    return published


async def prune_published(
    session: AsyncSession,
    model: type,
    *,
    older_than: datetime,
) -> int:
    """Delete published rows older than `older_than`. Returns rows removed."""
    result = await session.execute(
        delete(model).where(
            model.published_at.is_not(None),
            model.published_at < older_than,
        )
    )
    await session.commit()
    return result.rowcount or 0


async def run_outbox_relay(
    session_factory: async_sessionmaker[AsyncSession],
    get_nats: Callable[[], Any],
    model: type,
    *,
    name: str = "outbox",
    tick_seconds: float = 5.0,
    batch_size: int = 100,
    retention_seconds: float = 7 * 24 * 3600,
    prune_every_seconds: float = 3600.0,
) -> None:
    """Background relay: drain unpublished rows to JetStream, prune old ones.

    Mirrors the health scheduler's tick/backoff/cancellation shape. A tick with
    no NATS connection or a failed publish backs off exponentially to a cap; a
    healthy tick resets to `tick_seconds`. `get_nats` is called each tick so a
    reconnect (or a connection that was None at startup) is picked up. Drains all
    pending rows on the first healthy tick after an outage, which is what makes
    restart-recovery work. Cancellable via task.cancel().
    """
    max_backoff = max(tick_seconds * 10, 300)
    current_backoff = tick_seconds
    last_prune = datetime.now(timezone.utc)
    logger.info("%s relay started; tick=%ss batch=%s", name, tick_seconds, batch_size)
    while True:
        tick_failed = False
        try:
            nc = get_nats()
            if nc is None:
                # No NATS yet; back off and retry without touching the DB.
                tick_failed = True
            else:
                js = nc.jetstream()
                async with session_factory() as session:
                    count = await _publish_pending(session, js, model, batch_size=batch_size)
                if count:
                    logger.info("%s relay published %s event(s)", name, count)
                now = datetime.now(timezone.utc)
                if (now - last_prune).total_seconds() >= prune_every_seconds:
                    cutoff = now - timedelta(seconds=retention_seconds)
                    async with session_factory() as session:
                        removed = await prune_published(session, model, older_than=cutoff)
                    last_prune = now
                    if removed:
                        logger.info("%s relay pruned %s published event(s)", name, removed)
        except asyncio.CancelledError:
            logger.info("%s relay cancelled; exiting loop", name)
            raise
        except Exception:
            logger.exception("%s relay tick failed", name)
            tick_failed = True

        current_backoff = min(current_backoff * 2, max_backoff) if tick_failed else tick_seconds
        try:
            await asyncio.sleep(current_backoff)
        except asyncio.CancelledError:
            raise


def _stream_sequence_key(msg: Any) -> str | None:
    """`<stream>:<stream_sequence>` from JetStream metadata, or None."""
    try:
        meta = msg.metadata
        return f"{meta.stream}:{meta.sequence.stream}"
    except Exception:
        return None


def event_dedupe_key(event_data: dict[str, Any] | None, msg: Any) -> str | None:
    """Resolve the stable idempotency key for a consumed event.

    Prefers the producer-stamped `event_id` in the payload, which is stable
    across a relay republish under a new stream sequence. Falls back to the
    JetStream `<stream>:<sequence>` for events published before the outbox
    existed, so a rolling deploy stays correct.
    """
    if event_data:
        eid = event_data.get(EVENT_ID_FIELD)
        if eid:
            return str(eid)
    return _stream_sequence_key(msg)


__all__ = [
    "OutboxMixin",
    "enqueue_event",
    "prune_published",
    "run_outbox_relay",
    "event_dedupe_key",
    "NATS_MSG_ID_HEADER",
    "EVENT_ID_FIELD",
]
