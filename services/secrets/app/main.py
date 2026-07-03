from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine

# Import models so Base.metadata.create_all picks up the tables.
from app.models import KeyVersion, Secret  # noqa: F401
from app.routers.internal import router as internal_router
from app.routers.secrets import keys_router
from app.routers.secrets import router as secrets_router
from app.services.keyring import bootstrap_keyring

setup_logging("secrets", level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Refuse to boot without valid, matching key material (ADR 0003, decision
    # point 3): bootstrap_keyring raises KekError and startup fails here, before
    # the service can accept a write it could not encrypt or a read it could
    # not decrypt.
    async with AsyncSessionLocal() as session:
        app.state.keyring = await bootstrap_keyring(
            session,
            kek_encoded=settings.secrets_kek,
            previous_kek_encoded=settings.secrets_kek_previous,
        )
    yield


app = FastAPI(
    title="HERD Secrets Service",
    description="Encrypted-at-rest credential store",
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

app.include_router(secrets_router)
app.include_router(keys_router)
app.include_router(internal_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "secrets"}
