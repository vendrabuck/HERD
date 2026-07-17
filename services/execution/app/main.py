import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
    """Create HERD_HEALTH stream if NATS is up.

    Idempotent: add_stream returns the existing stream if it already
    exists. Non-fatal if NATS is down so the service still boots; the
    health scheduler simply drops publish attempts in that case.
    """
    nc = getattr(app.state, "nats", None)
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await js.add_stream(name="HERD_HEALTH", subjects=["herd.health.*"])
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

    Idempotent: add_stream returns the existing stream if it already exists.
    Non-fatal if NATS is down so the service still boots; DLQ publishes are
    best-effort and swallow their own errors in that case.
    """
    nc = getattr(app.state, "nats", None)
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await js.add_stream(name="HERD_DLQ", subjects=["herd.*.dlq.>"])
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
        log=logger,
    )
    await start_nats_consumer(app)
    await _ensure_health_stream(app)
    await _ensure_dlq_stream(app)
    await start_health_scheduler(app)
    await start_outbox_relay(app)
    await start_wiring_retry_scheduler(app)
    yield
    await stop_wiring_retry_scheduler(app)
    await stop_outbox_relay(app)
    await stop_health_scheduler(app)
    await stop_nats_consumer(app)


app = FastAPI(
    title="HERD Execution Service",
    description="Driver execution engine for infrastructure devices",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
