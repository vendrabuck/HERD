import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.models.user import Role
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router

setup_logging("auth")
logger = logging.getLogger(__name__)


async def _seed_superadmin() -> None:
    """
    Create the superadmin account on first startup if the three SUPERADMIN_*
    environment variables are all set and no superadmin already exists.
    This runs exactly once; subsequent startups are a no-op.
    """
    email = settings.superadmin_email.strip()
    username = settings.superadmin_username.strip()
    password = settings.superadmin_password.strip()

    if not (email and username and password):
        return

    from app.services.auth_service import (
        create_user,
        get_user_by_email,
        superadmin_exists,
    )

    async with AsyncSessionLocal() as db:
        if await superadmin_exists(db):
            return

        if await get_user_by_email(db, email):
            logger.warning(
                "SUPERADMIN_EMAIL '%s' is already registered as a regular user. "
                "Superadmin was not created.",
                email,
            )
            return

        await create_user(
            db, email=email, username=username, password=password, role=Role.SUPERADMIN
        )
        logger.info("Superadmin account created for '%s'.", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_superadmin()
    yield


app = FastAPI(
    title="HERD Auth Service",
    description="Authentication and authorization for HERD",
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

app.include_router(auth_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "auth"}
