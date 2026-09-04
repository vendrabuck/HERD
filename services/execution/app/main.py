import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.consumer_schema_gate import (
    start_consumer_when_schema_ready,
    stop_consumer_schema_gate,
)
from herd_common.cors import add_cors_middleware
from herd_common.jetstream import ensure_stream
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.outbox import run_outbox_relay
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.models import *  # noqa: F401, F403
from app.models.outbox import OutboxEvent
from app.routers.executions import router as executions_router
from app.routers.health import router as health_router
from app.routers.validation import router as validation_router
from app.services.health_scheduler import start_health_scheduler, stop_health_scheduler
from app.services.nats_consumer import start_nats_consumer, stop_nats_consumer
from app.services.wiring_retry_service import (
    start_wiring_retry_scheduler,
    stop_wiring_retry_scheduler,
)

setup_logging("execution", level=settings.log_level)
logger = logging.getLogger(__name__)


async def _ensure_health_stream(app: FastAPI) -> None:
    """Create or update the HERD_HEALTH stream if NATS is up.

    Idempotent: ensure_stream creates the stream if it does not exist, or
    updates it in place if it exists with a different configuration (e.g. a
    changed max_age on an upgraded-in-place stack); a matching existing
    config is a no-op. Non-fatal if NATS is down so the service still boots;
    the health scheduler simply drops publish attempts in that case.
    """
    nc = getattr(app.state, "nats", None)
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await ensure_stream(
            js,
            name="HERD_HEALTH",
            subjects=["herd.health.*"],
            max_age_seconds=settings.nats_stream_max_age_seconds,
        )
        logger.info("JetStream stream HERD_HEALTH ready")
    except Exception:
        logger.warning("Could not create HERD_HEALTH stream", exc_info=True)


async def _ensure_dlq_stream(app: FastAPI) -> None:
    """Create the shared HERD_DLQ stream if NATS is up.

    Every consumer routes poison and retry-exhausted messages to a 4-token
    DLQ subject (herd.reservations.dlq.execution, herd.reservations.dlq.notifications,
    herd.health.dlq.notifications). Those subjects are deliberately one token
    longer than any consumer's 3-token filter so a DLQ'd message is never
    redelivered to the consumer that failed it; the flip side is that no
    producing stream captures them, so without this stream _publish_to_dlq
    publishes into the void and the message is lost. The "herd.*.dlq.>" pattern
    binds all current and future DLQ subjects into one inspectable stream.

    Idempotent: ensure_stream creates the stream if it does not exist, or
    updates it in place if it exists with a different configuration (e.g. a
    changed max_age on an upgraded-in-place stack); a matching existing
    config is a no-op. Non-fatal if NATS is down so the service still boots;
    DLQ publishes are best-effort and swallow their own errors in that case.
    """
    nc = getattr(app.state, "nats", None)
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await ensure_stream(
            js,
            name="HERD_DLQ",
            subjects=["herd.*.dlq.>"],
            max_age_seconds=settings.nats_stream_max_age_seconds,
        )
        logger.info("JetStream stream HERD_DLQ ready")
    except Exception:
        logger.warning("Could not create HERD_DLQ stream", exc_info=True)


async def start_outbox_relay(app: FastAPI) -> None:
    """Start the transactional-outbox relay as a background task (issue #21).

    Drains unpublished OutboxEvent rows (staged by the health scheduler in the
    same transaction as the status update) to JetStream and prunes old ones.
    `get_nats` is read each tick via app.state.nats, so a NATS connection that
    was None at startup, or a later reconnect, is picked up without a restart.
    Mirrors start_health_scheduler: stored on app.state for shutdown to cancel.
    """
    task = asyncio.create_task(
        run_outbox_relay(
            AsyncSessionLocal,
            lambda: getattr(app.state, "nats", None),
            OutboxEvent,
            name="execution-outbox",
            tick_seconds=settings.outbox_relay_tick_seconds,
            batch_size=settings.outbox_batch_size,
            retention_seconds=settings.outbox_retention_seconds,
            engine=engine,
            wake_on_write=settings.outbox_wake_on_write,
        )
    )

    def _surface_crash(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("outbox relay task exited unexpectedly: %s", exc)

    task.add_done_callback(_surface_crash)
    app.state.outbox_relay_task = task


async def stop_outbox_relay(app: FastAPI) -> None:
    """Cancel the outbox relay task on app shutdown."""
    task = getattr(app.state, "outbox_relay_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _start_consumer_and_streams(app: FastAPI) -> None:
    """Start the NATS consumer, then ensure the streams its connection enables.

    The stream ensures read app.state.nats, which start_nats_consumer sets, so
    they must follow it. Grouped so the issue #463 schema gate preserves that
    ordering when consumer start is deferred past boot.
    """
    await start_nats_consumer(app)
    await _ensure_health_stream(app)
    await _ensure_dlq_stream(app)


@asynccontextmanager
async def lifespan(app: FastAPI):
    outcome = await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
        log=logger,
    )
    # Issue #463: on a managed schema missing this release's tables (boot before
    # `make migrate`), defer consumer start so events wait on the stream instead
    # of dead-lettering; everything below still starts as usual.
    await start_consumer_when_schema_ready(
        app,
        outcome,
        engine=engine,
        metadata=Base.metadata,
        schema=settings.db_schema,
        start_consumer=_start_consumer_and_streams,
        service="execution",
        log=logger,
    )
    await start_health_scheduler(app)
    await start_outbox_relay(app)
    await start_wiring_retry_scheduler(app)
    yield
    await stop_wiring_retry_scheduler(app)
    await stop_outbox_relay(app)
    await stop_health_scheduler(app)
    await stop_consumer_schema_gate(app)
    await stop_nats_consumer(app)


app = FastAPI(
    title="HERD Execution Service",
    description="Driver execution engine for infrastructure devices",
    version="0.1.0",
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

app.add_middleware(RequestLoggingMiddleware)


def mount_api_routers(target: FastAPI) -> bool:
    """Mount the HTTP API routers unless this replica runs poller-only.

    EXECUTION_POLLER_ONLY=true (issue #24) turns the replica into a background
    worker: the NATS consumer, health scheduler, and outbox relay all run, but
    only the bare /health liveness route is served. Consumed at startup, so the
    same image serves both roles; API replicas pair it with
    HEALTH_POLL_SCHEDULER_ENABLED=false for a clean split. Returns whether the
    routers were mounted.
    """
    if settings.execution_poller_only:
        logger.info("execution_poller_only set; HTTP API routers not mounted")
        return False
    target.include_router(executions_router)
    target.include_router(health_router)
    target.include_router(validation_router)
    return True


mount_api_routers(app)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "execution"}
