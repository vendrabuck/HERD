import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from herd_common.logging import RequestLoggingMiddleware, setup_logging

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.routers.apply_jobs import router as apply_jobs_router
from app.routers.bulk import router as bulk_router
from app.routers.device_configs import router as device_configs_router
from app.routers.device_groups import router as device_groups_router
from app.routers.devices import router as devices_router
from app.routers.drivers import router as drivers_router
from app.routers.ports import router as ports_router
from app.routers.templates import router as templates_router
from app.storage import init_storage

setup_logging("inventory", level=settings.log_level)
logger = logging.getLogger(__name__)


async def _seed_no_pool() -> None:
    """Create the 'No Pool' default device group on first startup."""
    from sqlalchemy.exc import IntegrityError

    from app.models.device_group import DeviceGroup
    from app.services.device_group_service import get_no_pool_group

    async with AsyncSessionLocal() as db:
        existing = await get_no_pool_group(db)
        if existing:
            return
        try:
            db.add(DeviceGroup(name="No Pool", description="Default group for unassigned devices"))
            await db.commit()
            logger.info("Default device group 'No Pool' created.")
        except IntegrityError:
            await db.rollback()
            logger.debug("'No Pool' group already exists.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_no_pool()
    init_storage()

    scheduler_task: asyncio.Task | None = None
    if settings.apply_scheduler_enabled:
        from app.services.apply_scheduler import run_scheduler_loop

        scheduler_task = asyncio.create_task(run_scheduler_loop(AsyncSessionLocal))

        def _log_scheduler_crash(task: asyncio.Task) -> None:
            # Surface a crash immediately rather than at process shutdown when
            # the finally block first awaits the task. Without this, a scheduler
            # that raises before its first `await` runs silently with no apply
            # jobs being processed until someone restarts the service.
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Apply scheduler task crashed", exc_info=exc)

        scheduler_task.add_done_callback(_log_scheduler_crash)
        logger.info("Apply scheduler task started")

    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(
    title="HERD Inventory Service",
    description="Lab equipment inventory management for HERD",
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
app.include_router(apply_jobs_router)
app.include_router(device_configs_router)
app.include_router(device_groups_router)
app.include_router(devices_router)
app.include_router(drivers_router)
app.include_router(ports_router)
app.include_router(templates_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "inventory"}
