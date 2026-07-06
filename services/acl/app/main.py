from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import Base, engine
from app.models.grant import ResourceGrant  # noqa: F401
from app.routers.grants import router as grants_router

setup_logging("acl", level=settings.log_level)


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
    title="HERD ACL Service",
    description="Access Control List service for HERD",
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

app.include_router(grants_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "acl"}
