"""
Cabling Service

Manages the physical and virtual backend connections between devices within a topology.
These connections are made by administrators and are not visible to end-users as raw
cable data; the frontend topology editor reflects them, but users cannot create or
delete cabling entries directly.

Also manages topology persistence (canvas layouts saved to database).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import Base, engine
from app.models import *  # noqa: F401, F403
from app.routes.bulk import router as bulk_router
from app.routes.connections import router as connections_router
from app.routes.fabric import router as fabric_router
from app.routes.pathfind import router as pathfind_router
from app.routes.templates import router as templates_router
from app.routes.topologies import router as topologies_router
from app.routes.versions import router as versions_router

setup_logging("cabling", level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="HERD Cabling Service",
    description="Backend connection management between lab devices",
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

app.include_router(bulk_router)
app.include_router(connections_router)
app.include_router(fabric_router)
app.include_router(pathfind_router)
app.include_router(templates_router)
app.include_router(topologies_router)
app.include_router(versions_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cabling"}
