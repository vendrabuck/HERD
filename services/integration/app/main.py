from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.consumer_schema_gate import (
    start_consumer_when_schema_ready,
    stop_consumer_schema_gate,
)
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import Base, engine

# Import models so Base.metadata.create_all picks up the webhook tables.
from app.models import WebhookDelivery, WebhookSubscription  # noqa: F401
from app.routers.reservations import router as reservations_router
from app.routers.webhooks import router as webhooks_router
from app.routers.webhooks import test_sink_router
from app.services.nats_consumer import start_nats_consumer, stop_nats_consumer

setup_logging("integration", level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The v1 facade is stateless, but the integration service owns its own schema
    # for the webhook subscription and delivery tables (issue #33, phase 4).
    outcome = await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
    )
    # Issue #463: defer consumer start while a managed schema is missing this
    # release's tables, so events wait on the stream instead of dead-lettering.
    await start_consumer_when_schema_ready(
        app,
        outcome,
        engine=engine,
        metadata=Base.metadata,
        schema=settings.db_schema,
        start_consumer=start_nats_consumer,
        service="integration",
    )
    yield
    await stop_consumer_schema_gate(app)
    await stop_nats_consumer(app)


app = FastAPI(
    title="HERD API v1",
    description="Versioned, stable external API facade for HERD",
    version="1.0.0",
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

app.include_router(reservations_router)
app.include_router(webhooks_router)
# Test-only delivery sink, gated to the dev/test stack (never prod).
if settings.webhook_test_sink_enabled:
    app.include_router(test_sink_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "integration"}
