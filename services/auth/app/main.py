import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging
from herd_common.schema_init import create_all_and_stamp

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.models.api_token import ApiToken  # noqa: F401
from app.models.group import GroupMember, UserGroup  # noqa: F401
from app.models.ldap_group_mapping import LdapGroupMapping  # noqa: F401
from app.models.ldap_sync_run import LdapSyncRun  # noqa: F401
from app.models.user import Role
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.groups import router as groups_router
from app.routers.internal import router as internal_router
from app.routers.ldap_sync import router as ldap_sync_router
from app.routers.tokens import router as tokens_router
from app.tasks.ldap_sync_loop import (
    effective_interval_seconds,
    ldap_group_sync_loop_enabled,
    ldap_sync_loop,
)

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

    # ADR 0011 phase 5: the LDAP group sync interval loop, started only when
    # both auth_method == "ldap" and ldap_group_sync_enabled are true (dark
    # by default even in LDAP mode, mirroring the sync-now and deactivation
    # opt-ins). Cancelled and awaited on shutdown, mirroring
    # services/reservations/app/main.py's expiration_task handling.
    ldap_sync_task = None
    if ldap_group_sync_loop_enabled():
        interval = effective_interval_seconds(settings.ldap_sync_interval_seconds)
        ldap_sync_task = asyncio.create_task(ldap_sync_loop(interval))

    yield

    if ldap_sync_task is not None:
        ldap_sync_task.cancel()
        try:
            await ldap_sync_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="HERD Auth Service",
    description="Authentication and authorization for HERD",
    version="0.1.0",
    lifespan=lifespan,
)

add_cors_middleware(app, settings.cors_origins)

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
