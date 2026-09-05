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
    await enqueue_event(session, OutboxEvent, "herd.reservations.created", payload)
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

from sqlalchemy import JSON, DateTime, Integer, String, Uuid, delete, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from herd_common.advisory_lock import is_postgres_dialect

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


def outbox_channel(model: type) -> str:
    """Postgres NOTIFY channel for `model`'s outbox table: herd_outbox_<schema>.

    Derived from the model's table schema (or "public" when unset), so the
    notify side (enqueue_event) and the listen side (_listen_for_wakeups) call
    this same helper and cannot drift apart.
    """
    schema = model.__table__.schema or "public"
    return f"herd_outbox_{schema}"


async def enqueue_event(
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

    On Postgres, also issues `SELECT pg_notify(channel, '')` on the same
    session so a running relay can wake immediately instead of waiting for its
    next tick (issue #682). This stays inside the caller's transaction:
    Postgres only delivers the notification once that transaction commits,
    and drops it if the transaction rolls back, so a wake is never sent for a
    write that never happened. Postgres also dedupes identical (channel,
    payload) notifies within one transaction, so N events staged in one
    commit produce a single wake. On any other dialect (SQLite unit tests)
    this is a no-op beyond the add.

    This is the only sanctioned way for request/handler code to raise a
    state-change event: publishing straight to NATS from that code path
    reopens the dual-write gap this module exists to close (see the module
    docstring), since a process death or broker outage after the state commit
    would then lose the event with no row recording it was ever owed.
    """
    eid = event_id or uuid.uuid4()
    body = {**payload, EVENT_ID_FIELD: str(eid)}
    session.add(model(id=eid, subject=subject, payload=body))
    if is_postgres_dialect(session.bind.dialect.name):
        await session.execute(
            text("SELECT pg_notify(:channel, '')"), {"channel": outbox_channel(model)}
        )
    return eid


async def _publish_pending(
    session: AsyncSession,
    js: Any,
    model: type,
    *,
    batch_size: int,
    publish_timeout: float = 10.0,
) -> int:
    """Publish up to `batch_size` unpublished rows, marking each sent.

    Claims one row at a time with FOR UPDATE SKIP LOCKED so multiple relay
    instances never publish the same row, holding the row lock only across that
    row's single publish. Each publish is bounded by `publish_timeout`: a
    JetStream publish on a connection that drops mid-call can otherwise block the
    whole relay loop indefinitely, so on timeout we treat it as a failed attempt.
    A publish failure records the attempt, leaves the row unpublished for a later
    tick, and stops the batch (NATS is likely down, so there is no value
    hammering the rest this tick). Returns the count published.
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
            await asyncio.wait_for(
                js.publish(
                    row.subject,
                    json.dumps(row.payload, default=str).encode(),
                    headers={NATS_MSG_ID_HEADER: str(row.id)},
                ),
                timeout=publish_timeout,
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


async def _listen_for_wakeups(
    engine: AsyncEngine,
    channel: str,
    wake: asyncio.Event,
    *,
    retry_seconds: float,
) -> None:
    """Background task: LISTEN on `channel`, setting `wake` on every NOTIFY.

    Runs on a dedicated asyncpg connection held for the process lifetime: a
    LISTEN connection must not cycle through a SQLAlchemy pool's
    reset-on-return, and holding one out of the pool that long would starve
    other callers. asyncpg is imported lazily: herd_common has no asyncpg
    dependency of its own and must not gain one; the two services that run
    the relay already depend on it as their SQLAlchemy driver.

    A connect or registration failure, or a lost connection, is logged and
    retried after `retry_seconds` in BOTH cases: every iteration sleeps that
    long before its next reconnect attempt, so a pooler, `idle_session_timeout`,
    or reaper that terminates the idle LISTEN session cannot turn this into a
    tight reconnect loop (each flap would otherwise re-run a full
    `_publish_pending`-triggering catch-up wake with no backoff at all). Every
    iteration also closes the connection it opened (if any) before the next
    one is opened, whether that iteration ended in a lost connection, a
    registration failure, or cancellation, so a flapping session cannot leak a
    backend per cycle. `wake` is set once per iteration, and only after
    `add_listener`/`add_termination_listener` both succeed (catch-up): so
    anything committed while we were disconnected (including before the first
    connect) drains on the relay's very next pass instead of waiting for a
    NOTIFY that will never come for an already-committed row, and a failed
    registration never claims a catch-up it did not earn.

    The connection is opened from kwargs derived straight from the engine's
    own dialect (`engine.dialect.create_connect_args`), not a re-rendered DSN
    string: the SQLAlchemy URL's query string uses spellings
    (`ssl`, `prepared_statement_cache_size`) that asyncpg's DSN parser does not
    accept as startup parameters, so a rendered-string DSN silently breaks any
    TLS or pgbouncer deployment that sets them. A handful of asyncpg-driver-only
    keys the dialect adds for its own pool wrapper (`async_fallback`,
    `async_creator_fn`, `prepared_statement_cache_size`,
    `prepared_statement_name_func`) are not accepted by `asyncpg.connect`
    itself and are popped before the call.
    """
    import asyncpg

    _, opts = engine.dialect.create_connect_args(engine.url)
    for key in (
        "async_fallback",
        "async_creator_fn",
        "prepared_statement_cache_size",
        "prepared_statement_name_func",
    ):
        opts.pop(key, None)

    while True:
        conn = None
        try:
            conn = await asyncpg.connect(**opts)

            closed = asyncio.Event()

            def _on_notify(connection: Any, pid: Any, notify_channel: Any, payload: Any) -> None:
                wake.set()

            def _on_terminate(connection: Any) -> None:
                closed.set()

            await conn.add_listener(channel, _on_notify)
            conn.add_termination_listener(_on_terminate)
            wake.set()
            logger.info("outbox listener: listening for writes on channel %s", channel)
            await closed.wait()
            logger.warning("outbox listener: lost the listen connection for channel %s", channel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "outbox listener: %s: %s (channel %s); retrying in %ss",
                type(exc).__name__,
                exc,
                channel,
                retry_seconds,
            )
        finally:
            if conn is not None:
                try:
                    await asyncio.wait_for(conn.close(), 5)
                except Exception:
                    pass

        await asyncio.sleep(retry_seconds)


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
    publish_timeout: float = 10.0,
    engine: AsyncEngine | None = None,
    wake_on_write: bool = True,
    wake: asyncio.Event | None = None,
) -> None:
    """Background relay: drain unpublished rows to JetStream, prune old ones.

    Mirrors the health scheduler's tick/backoff/cancellation shape. `get_nats` is
    called each tick so a reconnect (or a connection that was None at startup) is
    picked up. The connection must report `is_connected` before we publish: a
    JetStream publish on a disconnected client can block the loop, and the
    services connect with infinite reconnect, so during a broker outage the relay
    just waits at the base cadence (not escalating backoff) until the client
    reconnects, then drains all buffered rows on the first healthy tick. That
    drain-on-recovery is what makes restart-recovery work. An unexpected error
    backs off exponentially to a cap. Cancellable via task.cancel().

    Issue #682: on Postgres, `enqueue_event` also does `pg_notify` on the
    committing transaction. When `wake_on_write` is set (the default) and
    `engine` is a Postgres engine, this relay starts a supervised
    `_listen_for_wakeups` task that sets `wake` on every notification, and a
    healthy tick waits on `wake` (bounded by `tick_seconds`) instead of
    sleeping blindly, so a committed write is drained immediately instead of
    waiting out the rest of the tick. The tick remains the fallback cadence:
    a missed or delayed notification, a listener that has not (re)connected
    yet, or any dialect other than Postgres still drains on the next tick.
    `wake_on_write=False` is the ops escape hatch back to tick-only behavior.
    `wake` is a test seam: passing one in (even with no `engine`) tells the
    relay to honor it on a healthy tick exactly as it would a live listener,
    which is how the SQLite unit tests exercise the wake path without a real
    Postgres LISTEN connection; when omitted, the relay creates its own
    Event and only a real listener task can ever set it.
    """
    wake_provided = wake is not None
    if wake is None:
        wake = asyncio.Event()

    channel = outbox_channel(model)
    listener_task: asyncio.Task | None = None
    if wake_on_write and engine is not None and is_postgres_dialect(engine.dialect.name):
        listener_task = asyncio.create_task(
            _listen_for_wakeups(engine, channel, wake, retry_seconds=tick_seconds)
        )

    max_backoff = max(tick_seconds * 10, 300)
    current_backoff = tick_seconds
    last_prune = datetime.now(timezone.utc)
    logger.info("%s relay started; tick=%ss batch=%s", name, tick_seconds, batch_size)
    try:
        while True:
            # Clear BEFORE the drain (the lost-wakeup rule): a notify that
            # arrives during this iteration's drain sets the event again, so
            # the wait below returns immediately and the row that triggered
            # it is picked up on the very next pass rather than waiting a
            # full tick.
            wake.clear()
            tick_failed = False
            waiting_for_nats = False
            saturated = False
            try:
                nc = get_nats()
                if nc is None or not nc.is_connected:
                    # Not connected (startup, or an outage we are waiting out). Retry
                    # at the base cadence so reconnection is picked up promptly, and
                    # do not call publish on a dead client.
                    waiting_for_nats = True
                else:
                    js = nc.jetstream()
                    async with session_factory() as session:
                        count = await _publish_pending(
                            session,
                            js,
                            model,
                            batch_size=batch_size,
                            publish_timeout=publish_timeout,
                        )
                    if count:
                        logger.info("%s relay published %s event(s)", name, count)
                    saturated = count == batch_size
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

            if not waiting_for_nats and not tick_failed and saturated:
                # The drain claimed a full batch: Postgres collapses one
                # NOTIFY per committing transaction, so a single commit that
                # staged more than batch_size rows gets exactly one wake and
                # more rows likely remain behind this one. Loop immediately
                # rather than waiting out any part of a tick. Skipping
                # straight to the top of the loop (instead of, say, sleeping
                # zero) means wake.clear() runs before the next drain exactly
                # as it does on any other iteration, so the lost-wakeup rule
                # still holds: a notify arriving during this next drain is
                # not lost.
                continue

            if waiting_for_nats:
                current_backoff = tick_seconds
            else:
                current_backoff = (
                    min(current_backoff * 2, max_backoff) if tick_failed else tick_seconds
                )

            # A healthy tick with a wake mechanism in play (a live listener
            # task, or the wake seam) waits on the event so a write can cut
            # the wait short; otherwise (NATS down, or a failed tick in
            # exponential backoff) sleep plainly, so a burst of writes during
            # an outage cannot turn the backoff into a hot loop.
            listener_active = wake_provided or listener_task is not None
            try:
                if not waiting_for_nats and not tick_failed and listener_active:
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=current_backoff)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(current_backoff)
            except asyncio.CancelledError:
                raise
    finally:
        if listener_task is not None:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass


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
    "outbox_channel",
    "prune_published",
    "run_outbox_relay",
    "event_dedupe_key",
    "NATS_MSG_ID_HEADER",
    "EVENT_ID_FIELD",
]
