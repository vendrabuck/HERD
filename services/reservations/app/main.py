import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.jetstream import ensure_stream
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.outbox import run_outbox_relay
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.models.outbox import OutboxEvent
from app.routers.purpose_review import router as purpose_review_router
from app.routers.reservations import router as reservations_router

setup_logging("reservations", level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
        log=logger,
    )

    # Connect to NATS (non-fatal if unavailable)
    app.state.nats = None
    try:
        import nats

        # Retry reconnect forever (max_reconnect_attempts=-1): the outbox relay
        # depends on this connection recovering after a broker restart, otherwise
        # buffered events would be stranded once the default 60-attempt cap gave
        # up and closed the connection.
        nc = await nats.connect(
            settings.nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        app.state.nats = nc
        logger.info("Connected to NATS at %s", settings.nats_url)
        js = nc.jetstream()
        await ensure_stream(
            js,
            name="HERD_RESERVATIONS",
            subjects=["herd.reservations.*"],
            max_age_seconds=settings.nats_stream_max_age_seconds,
        )
        logger.info("JetStream stream HERD_RESERVATIONS ready")
    except Exception:
        logger.warning("NATS unavailable at %s, events will be skipped", settings.nats_url)

    # Start expiration background task
    from app.tasks.expiration import expiration_loop

    expiration_task = asyncio.create_task(expiration_loop(settings.expiration_interval_seconds))

    # Start the transactional-outbox relay (issue #21): drain unpublished outbox
    # rows to JetStream and prune old ones. get_nats is read each tick so a
    # reconnect (or a connection that was None at startup) is picked up.
    outbox_task = asyncio.create_task(
        run_outbox_relay(
            AsyncSessionLocal,
            lambda: getattr(app.state, "nats", None),
            OutboxEvent,
            name="reservations-outbox",
            tick_seconds=settings.outbox_relay_tick_seconds,
            batch_size=settings.outbox_batch_size,
            retention_seconds=settings.outbox_retention_seconds,
        )
    )

    yield

    # Cancel background tasks
    expiration_task.cancel()
    try:
        await expiration_task
    except asyncio.CancelledError:
        pass

    outbox_task.cancel()
    try:
        await outbox_task
    except asyncio.CancelledError:
        pass

    # Close NATS connection on shutdown
    if app.state.nats is not None:
        try:
            await app.state.nats.close()
        except Exception:
            logger.warning("Error closing NATS connection", exc_info=True)


app = FastAPI(
    title="HERD Reservations Service",
    description="Lab equipment reservation management for HERD",
    version="0.1.0",
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

app.add_middleware(RequestLoggingMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "reservations"}


app.include_router(reservations_router)
app.include_router(purpose_review_router)
