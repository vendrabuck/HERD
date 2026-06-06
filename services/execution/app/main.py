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


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await start_nats_consumer(app)
    await _ensure_health_stream(app)
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
