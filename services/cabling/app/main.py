"""
Cabling Service

Manages the physical and virtual backend connections between devices within a topology.
These connections are made by administrators and are not visible to end-users as raw
cable data; the frontend topology editor reflects them, but users cannot create or
delete cabling entries directly.

Also manages topology persistence (canvas layouts saved to database).
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import Base, engine
from app.models import *  # noqa: F401, F403
from app.routes.bulk import router as bulk_router
from app.routes.connections import router as connections_router
from app.routes.fabric import router as fabric_router
from app.routes.forks import router as forks_router
from app.routes.pathfind import router as pathfind_router
from app.routes.templates import router as templates_router
from app.routes.topologies import router as topologies_router
from app.routes.versions import router as versions_router

setup_logging("cabling", level=settings.log_level)


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
    title="HERD Cabling Service",
    description="Backend connection management between lab devices",
    version="0.1.0",
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

app.add_middleware(RequestLoggingMiddleware)

app.include_router(bulk_router)
app.include_router(connections_router)
app.include_router(fabric_router)
app.include_router(forks_router)
app.include_router(pathfind_router)
app.include_router(templates_router)
app.include_router(topologies_router)
app.include_router(versions_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cabling"}
