from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import Base, engine
from app.models.preferences import UserPreferences  # noqa: F401
from app.routers.preferences import router as preferences_router

setup_logging("user-profile", level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
    )
    yield


app = FastAPI(
    title="HERD User Profile Service",
    description="User preferences and saved filters for HERD",
    version="0.1.0",
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

app.add_middleware(RequestLoggingMiddleware)

app.include_router(preferences_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "user-profile"}
