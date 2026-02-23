import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import Base, engine
from app.routers.reservations import router as reservations_router

setup_logging("reservations")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Connect to NATS (non-fatal if unavailable)
    app.state.nats = None
    try:
        import nats

        nc = await nats.connect(settings.nats_url)
        app.state.nats = nc
        logger.info("Connected to NATS at %s", settings.nats_url)
    except Exception:
        logger.warning("NATS unavailable at %s, events will be skipped", settings.nats_url)

    # Start expiration background task
    from app.tasks.expiration import expiration_loop

    expiration_task = asyncio.create_task(
        expiration_loop(settings.expiration_interval_seconds)
    )

    yield

    # Cancel expiration task
    expiration_task.cancel()
    try:
        await expiration_task
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RequestLoggingMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "reservations"}


app.include_router(reservations_router)
