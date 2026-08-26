"""Gate NATS consumer start on schema readiness (issue #463).

Since issue #419 a migration-managed schema is never touched at boot, so the
documented upgrade order (recreate containers, then ``make migrate``) has a
window where a service runs against a schema missing the new release's tables.
API routes tolerate that window (a request fails loudly and the caller
retries), but a NATS consumer does not: a handler that hits a missing table
NAKs through the backoff schedule and dead-letters the event roughly 81
seconds later, and a dead-lettered ``reservation.wiring_changed`` has no
automatic re-stage (the reservations-side ledger advanced in the same commit
as the outbox row, so the sweep heal never fires).

``start_consumer_when_schema_ready`` starts the consumer immediately unless
the ``SchemaInitOutcome`` reports model tables missing on the managed path.
When gated, it leaves the consumer stopped, which keeps events pending on the
stream with the durable consumer's offset intact (the at-least-once behavior
the outbox was built for), and polls the same missing-tables check in the
background, warning loudly on a period, then starts the consumer as soon as
the tables exist: ``make migrate`` heals a waiting consumer without a
container restart. Everything else in the lifespan (API routers, health
endpoints, outbox relay, schedulers) starts exactly as before, and
fresh-bootstrap and legacy schemas are never gated because ``create_all``
still runs there.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine

from herd_common.schema_init import SchemaInitOutcome, missing_model_tables

logger = logging.getLogger(__name__)

_GATE_TASK_ATTR = "consumer_schema_gate_task"


async def start_consumer_when_schema_ready(
    app: Any,
    outcome: SchemaInitOutcome,
    *,
    engine: AsyncEngine,
    metadata: MetaData,
    schema: str | None,
    start_consumer: Callable[[Any], Awaitable[None]],
    service: str,
    log: logging.Logger | None = None,
    poll_interval_seconds: float = 5.0,
    warn_interval_seconds: float = 60.0,
) -> None:
    """Start the consumer now, or defer it behind a schema-readiness poll.

    ``start_consumer`` is the service's ``start_nats_consumer`` (or a wrapper
    around it); it receives ``app``. On the ungated path it is awaited inline,
    so a failure there propagates exactly as it did before this gate existed.
    On the gated path the poll task is stored on
    ``app.state.consumer_schema_gate_task``; pair with
    ``stop_consumer_schema_gate`` at shutdown.

    Cancellation safety (issue #534): ``stop_consumer_schema_gate`` cancels
    the poll task, and a cancel delivered while the readiness query is
    mid-flight inside SQLAlchemy's async adapter used to drop the raw
    aiosqlite connection unclosed (the adapter's ``terminate()`` re-raises
    ``CancelledError`` instead of closing), later surfacing as an unraisable
    ``Connection.__del__`` warning on an unrelated test. Plain
    ``asyncio.shield()`` is not sufficient by itself: the outer ``await``
    still raises ``CancelledError`` the instant the enclosing task is
    cancelled regardless of whether the shielded query has finished, which
    left the query running detached with nothing awaiting it, so a caller
    that then closed the event loop (a test fixture's teardown) raced the
    still-running query and surfaced as a
    ``PytestUnhandledThreadExceptionWarning`` instead of the original leak.
    ``_poll_until_ready`` therefore runs the readiness query as its own
    task, shields that task, and on a ``CancelledError`` explicitly waits
    for the shielded task to finish before re-raising, so shutdown always
    waits for at most one in-flight query to close its connection cleanly.
    """
    log = log or logger
    if not outcome.consumer_should_wait:
        await start_consumer(app)
        return

    log.warning(
        "Service '%s': schema is migration-managed but model tables %s are missing "
        "(a boot before `make migrate`). NATS consumer start is DEFERRED so events "
        "stay pending on the stream instead of dead-lettering; the consumer starts "
        "automatically once the tables exist. Run `make migrate` (or "
        "`make migrate-%s`).",
        service,
        sorted(outcome.missing_model_tables),
        service,
    )

    async def _poll_until_ready() -> None:
        last_warned = time.monotonic()
        while True:
            await asyncio.sleep(poll_interval_seconds)
            try:
                # Cancellation-safe readiness check (issue #534): a shielded
                # task, waited to completion on cancel. See the "Cancellation
                # safety" paragraph in start_consumer_when_schema_ready's
                # docstring for why plain asyncio.shield() alone is not
                # sufficient here.
                query_task = asyncio.ensure_future(
                    missing_model_tables(engine, metadata, schema=schema)
                )
                try:
                    still_missing = await asyncio.shield(query_task)
                except asyncio.CancelledError:
                    if not query_task.done():
                        await asyncio.wait([query_task])
                    raise
            except Exception as exc:  # transient DB trouble must not kill the gate
                log.warning(
                    "Service '%s': consumer schema gate could not check readiness "
                    "(%s); will retry.",
                    service,
                    exc,
                )
                continue
            if not still_missing:
                await start_consumer(app)
                log.info(
                    "Service '%s': schema is ready; deferred NATS consumer started.",
                    service,
                )
                return
            now = time.monotonic()
            if now - last_warned >= warn_interval_seconds:
                log.warning(
                    "Service '%s': NATS consumer still waiting on missing model "
                    "tables %s; events are pending on the stream, none are being "
                    "processed. Run `make migrate`.",
                    service,
                    sorted(still_missing),
                )
                last_warned = now

    task = asyncio.create_task(_poll_until_ready())

    def _surface_crash(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error(
                "Service '%s': consumer schema gate task exited unexpectedly: %s",
                service,
                exc,
            )

    task.add_done_callback(_surface_crash)
    setattr(app.state, _GATE_TASK_ATTR, task)


async def stop_consumer_schema_gate(app: Any) -> None:
    """Cancel a pending schema gate task at shutdown. Safe when none was started.

    The poll loop shields its readiness query (issue #534), so this can take
    up to one in-flight ``missing_model_tables`` call to actually finish
    before the cancellation is observed here; that is deliberate; see
    ``_poll_until_ready``.
    """
    task = getattr(app.state, _GATE_TASK_ATTR, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
