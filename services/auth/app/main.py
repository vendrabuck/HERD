import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.models.api_token import ApiToken  # noqa: F401
from app.models.group import GroupMember, UserGroup  # noqa: F401
from app.models.ldap_group_mapping import LdapGroupMapping  # noqa: F401
from app.models.user import Role
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.groups import router as groups_router
from app.routers.internal import router as internal_router
from app.routers.ldap_sync import router as ldap_sync_router
from app.routers.tokens import router as tokens_router

setup_logging("auth", level=settings.log_level)
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


async def _seed_not_grouped() -> None:
    """Create the 'Not Grouped' default user group on first startup."""
    from sqlalchemy.exc import IntegrityError

    from app.services.group_service import get_group_by_name

    async with AsyncSessionLocal() as db:
        existing = await get_group_by_name(db, "Not Grouped")
        if existing:
            return
        try:
            db.add(UserGroup(name="Not Grouped", description="Default group for unassigned users"))
            await db.commit()
            logger.info("Default user group 'Not Grouped' created.")
        except IntegrityError:
            await db.rollback()
            logger.debug("'Not Grouped' group already exists.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_and_stamp(
        engine,
        Base.metadata,
        schema=settings.db_schema,
        script_location=Path(__file__).resolve().parents[1] / "migrations",
        log=logger,
    )
    await _seed_superadmin()
    await _seed_not_grouped()
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
app.include_router(groups_router)
app.include_router(internal_router)
app.include_router(ldap_sync_router)
app.include_router(tokens_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "auth"}
