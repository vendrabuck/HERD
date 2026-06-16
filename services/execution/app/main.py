import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import Base, engine
from app.models import *  # noqa: F401, F403
from app.routers.executions import router as executions_router
from app.routers.health import router as health_router
from app.services.health_scheduler import start_health_scheduler, stop_health_scheduler
from app.services.nats_consumer import start_nats_consumer, stop_nats_consumer

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await start_nats_consumer(app)
    await _ensure_health_stream(app)
    await _ensure_dlq_stream(app)
    await start_health_scheduler(app)
    yield
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

app.include_router(executions_router)
app.include_router(health_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "execution"}
