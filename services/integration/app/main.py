from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import Base, engine
from app.routers.reservations import router as reservations_router

setup_logging("integration", level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The facade itself is stateless, but the integration service owns its own
    # schema so phase 4 (webhooks) can add tables cleanly. create_all is a no-op
    # while no models are defined.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "integration"}
