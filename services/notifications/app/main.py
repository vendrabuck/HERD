from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import Base, engine
from app.models.notification import Notification  # noqa: F401
from app.models.outbound_delivery import OutboundDelivery  # noqa: F401
from app.routers.notifications import router as notifications_router
from app.services.nats_consumer import start_nats_consumer, stop_nats_consumer

setup_logging("notifications", level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
    )
    await start_nats_consumer(app)
    yield
    await stop_nats_consumer(app)


app = FastAPI(
    title="HERD Notifications Service",
    description="Event-driven in-app notifications for HERD",
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

app.include_router(notifications_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notifications"}
