"""Background task that auto-activates and auto-completes reservations."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select

from app.database import AsyncSessionLocal
from app.models.reservation import Reservation, ReservationStatus
from app.services.reservation_service import (
    _fetch_devices_best_effort,
    _update_device_statuses,
)

logger = logging.getLogger(__name__)


async def _run_expiration_cycle() -> None:
    """Single expiration cycle: activate pending, complete expired."""
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        # Activate PENDING reservations whose start_time has passed
        result = await db.execute(
            select(Reservation).where(
                and_(
                    Reservation.status == ReservationStatus.PENDING,
                    Reservation.start_time <= now,
                )
            )
        )
        pending = result.scalars().all()
        for res in pending:
            res.status = ReservationStatus.ACTIVE
            logger.info(
                "Auto-activated reservation %s",
                res.id,
                extra={"action": "auto_activate", "reservation_id": str(res.id)},
            )

        # Complete ACTIVE reservations whose end_time has passed
        result = await db.execute(
            select(Reservation).where(
                and_(
                    Reservation.status == ReservationStatus.ACTIVE,
                    Reservation.end_time <= now,
                )
            )
        )
        expired = result.scalars().all()
        for res in expired:
            res.status = ReservationStatus.COMPLETED
            logger.info(
                "Auto-completed reservation %s",
                res.id,
                extra={"action": "auto_complete", "reservation_id": str(res.id)},
            )

        await db.commit()

    # Release only exclusive devices for completed reservations. Uses the same
    # best-effort fetch + internal-token status update as the cancel/release
    # paths.
    for res in expired:
        device_ids = list(res.device_ids)
        fetch_results = await _fetch_devices_best_effort(device_ids)
        exclusive_ids: list[uuid.UUID] = []
        for did, result in zip(device_ids, fetch_results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Auto-expire: could not fetch device %s; assuming exclusive",
                    did,
                    exc_info=result,
                )
                exclusive_ids.append(did)
            elif result.get("exclusive", True):
                exclusive_ids.append(did)
        if exclusive_ids:
            await _update_device_statuses(exclusive_ids, "AVAILABLE")


async def expiration_loop(interval_seconds: int = 60) -> None:
    """Run expiration cycles forever at the given interval."""
    logger.info("Expiration loop started, interval=%ds", interval_seconds)
    while True:
        try:
            await _run_expiration_cycle()
        except Exception:
            logger.error("Expiration cycle failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
