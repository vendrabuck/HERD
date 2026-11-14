import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.jetstream import connect_nats, ensure_stream
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.outbox import run_outbox_relay
from herd_common.schema_init import create_all_and_stamp
from herd_common.version import add_version_route, service_version

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
        # Bounded first connect, unlimited reconnects once connected (issue
        # #1083, herd_common.jetstream.connect_nats): a broker that is down at
        # boot raises after a few tries, so this lifespan reaches the warning
        # below instead of waiting forever; an established connection still
        # retries forever, since the outbox relay depends on it recovering after
        # a broker restart.
        nc = await connect_nats(settings.nats_url)
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
        # Nothing assigns app.state.nats after this lifespan, so the two failures
        # differ (RES-EVENT-4, OPS-NATS-1). A failed connect leaves no connection
        # for the life of the process: the outbox relay publishes nothing and
        # events wait in the outbox until a restart with the broker reachable.
        # A connect that succeeded but whose stream ensure failed keeps the
        # connection, and the relay publishes. The ensure fails on a create of a
        # missing stream or on an update of an existing one (execution may have
        # created it): with the stream there, publishes succeed at once; while it
        # is missing, each publish fails and its row stays in the outbox and is
        # retried every tick.
        if app.state.nats is None:
            logger.warning(
                "NATS unavailable at %s; events wait in the outbox until the service "
                "is restarted with the broker reachable",
                settings.nats_url,
            )
        else:
            logger.warning(
                "JetStream stream HERD_RESERVATIONS could not be created or updated at "
                "%s; the connection is kept, so events publish if the stream exists, "
                "and while it is missing they wait in the outbox and the relay retries "
                "them every tick",
                settings.nats_url,
            )

    # Start expiration background task
    from app.tasks.expiration import expiration_loop, purpose_classify_loop

    expiration_task = asyncio.create_task(expiration_loop(settings.expiration_interval_seconds))

    # Start the purpose-classify reconciler on its own task, at its own
    # interval (issue #702): it is the only sweep reconciler bound by an LLM
    # call rather than a DB or fast HTTP round trip, so a slow or hung
    # orchestrator must not delay expiration_task's other reconcilers
    # (activation, auto-completion, both provisioning-timeout backstops, fork
    # archiving, wiring heal, and pending prune) behind it.
    purpose_classify_task = asyncio.create_task(
        purpose_classify_loop(settings.purpose_classify_interval_seconds)
    )

    # Start the transactional-outbox relay (issue #21): drain unpublished outbox
    # rows to JetStream and prune old ones. get_nats is read each tick, so an
    # established connection that drops is waited out until the client
    # reconnects. Nothing assigns app.state.nats after the connect above: when
    # that connect failed it stays None, and events wait in the outbox until the
    # service is restarted with the broker reachable (RES-EVENT-4).
    outbox_task = asyncio.create_task(
        run_outbox_relay(
            AsyncSessionLocal,
            lambda: getattr(app.state, "nats", None),
            OutboxEvent,
            name="reservations-outbox",
            tick_seconds=settings.outbox_relay_tick_seconds,
            batch_size=settings.outbox_batch_size,
            retention_seconds=settings.outbox_retention_seconds,
            engine=engine,
            wake_on_write=settings.outbox_wake_on_write,
        )
    )

    yield

    # Cancel background tasks
    expiration_task.cancel()
    try:
        await expiration_task
    except asyncio.CancelledError:
        pass

    purpose_classify_task.cancel()
    try:
        await purpose_classify_task
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
    version=service_version("herd-reservations"),
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

app.add_middleware(RequestLoggingMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "reservations"}


add_version_route(app, service="reservations", distribution="herd-reservations")

app.include_router(reservations_router)
app.include_router(purpose_review_router)
