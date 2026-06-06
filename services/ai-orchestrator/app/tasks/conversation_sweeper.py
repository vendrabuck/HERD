"""Background task that expires idle assistant conversations.

Mirrors the pattern in services/reservations/app/tasks/expiration.py: a
forever loop with a configurable interval, swallowing per-cycle exceptions
so a transient DB issue does not kill the task.
"""

import asyncio
import logging

from app.config import settings
from app.database import AsyncSessionLocal
from app.services import conversation_repo

logger = logging.getLogger(__name__)


async def _run_sweeper_cycle() -> int:
    async with AsyncSessionLocal() as db:
        return await conversation_repo.expire_idle(
            db, ttl_hours=settings.assistant_conversation_ttl_hours
        )


async def conversation_sweeper_loop(interval_seconds: int = 3600) -> None:
    """Run sweeper cycles forever at the given interval."""
    logger.info("conversation_sweeper_started", extra={"interval_seconds": interval_seconds})
    while True:
        try:
            expired = await _run_sweeper_cycle()
            if expired:
                logger.info("conversation_sweeper_cycle", extra={"expired": expired})
        except Exception:
            logger.error("conversation_sweeper_failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
