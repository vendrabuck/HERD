"""Background task that auto-activates and auto-completes reservations."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.reservation import Reservation, ReservationStatus

logger = logging.getLogger(__name__)


async def _update_device_statuses_internal(device_ids: list[uuid.UUID], status: str) -> None:
    """Best-effort device status update using internal token (no user JWT needed)."""
    if not settings.internal_api_token:
        return

    import httpx

    async with httpx.AsyncClient() as client:
        for device_id in device_ids:
            try:
                await client.post(
                    f"{settings.inventory_service_url}/devices/{device_id}/status",
                    json={"status": status},
                    headers={"X-Internal-Token": settings.internal_api_token},
                    timeout=10.0,
                )
            except Exception:
                logger.error(
                    "Failed to update device %s status to %s", device_id, status,
                    exc_info=True,
                )


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
                "Auto-activated reservation %s", res.id,
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
                "Auto-completed reservation %s", res.id,
                extra={"action": "auto_complete", "reservation_id": str(res.id)},
            )

        await db.commit()

    # Release devices for completed reservations (best-effort, outside DB session)
    for res in expired:
        device_ids = [uuid.UUID(d) for d in res.device_ids]
        await _update_device_statuses_internal(device_ids, "AVAILABLE")


async def expiration_loop(interval_seconds: int = 60) -> None:
    """Run expiration cycles forever at the given interval."""
    logger.info("Expiration loop started, interval=%ds", interval_seconds)
    while True:
        try:
            await _run_expiration_cycle()
        except Exception:
            logger.error("Expiration cycle failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
