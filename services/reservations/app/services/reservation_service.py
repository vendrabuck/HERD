"""
Reservation service: core business logic.

Key rules enforced here:
1. All requested devices must exist in the inventory service.
2. All devices must share the same topology_type (no mixing PHYSICAL + CLOUD).
3. No time-window overlap with existing active reservations for the same devices.
4. On success, emit a NATS event to notify downstream services.
5. On create/cancel/release, update device statuses in inventory (best-effort).
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime

import httpx
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.schemas.reservation import ReservationCreate

logger = logging.getLogger(__name__)


async def _fetch_devices(device_ids: list[uuid.UUID], token: str) -> list[dict]:
    """Fetch device info from Inventory service concurrently."""
    async with httpx.AsyncClient() as client:

        async def fetch_one(device_id: uuid.UUID) -> dict:
            resp = await client.get(
                f"{settings.inventory_service_url}/devices/{device_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            if resp.status_code == 404:
                raise ValueError(f"Device {device_id} not found in inventory")
            resp.raise_for_status()
            return resp.json()

        return await asyncio.gather(*[fetch_one(did) for did in device_ids])


async def _check_conflicts(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    start_time: datetime,
    end_time: datetime,
    exclude_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """
    Returns a list of device_ids that have conflicting reservations in the given window.
    Conflict = any ACTIVE/PENDING reservation overlapping [start_time, end_time).
    """
    query = select(Reservation).where(
        and_(
            Reservation.status.in_([ReservationStatus.ACTIVE, ReservationStatus.PENDING]),
            Reservation.start_time < end_time,
            Reservation.end_time > start_time,
        )
    )
    if exclude_id:
        query = query.where(Reservation.id != exclude_id)

    result = await db.execute(query)
    conflicting_reservations = result.scalars().all()

    # device_ids stored as JSON strings; normalize for comparison
    requested_str = {str(d) for d in device_ids}
    conflicting_devices: list[uuid.UUID] = []

    for res in conflicting_reservations:
        existing_str = {str(d) for d in res.device_ids}
        overlap = requested_str & existing_str
        conflicting_devices.extend(uuid.UUID(d) for d in overlap)

    return list(set(conflicting_devices))


async def _publish_nats_event(nc, event: dict) -> None:
    """Publish a NATS event using the provided connection. Errors are logged, never raised."""
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await js.publish(
            "herd.reservations.created",
            json.dumps(event, default=str).encode(),
        )
    except Exception:
        logger.error("Failed to publish NATS event: %s", event.get("event"), exc_info=True)


async def _update_device_statuses(
    device_ids: list[uuid.UUID], status: str, token: str
) -> None:
    """Best-effort update of device statuses in the inventory service."""
    if not settings.internal_api_token:
        return

    async with httpx.AsyncClient() as client:

        async def update_one(device_id: uuid.UUID) -> None:
            try:
                await client.post(
                    f"{settings.inventory_service_url}/devices/{device_id}/status",
                    json={"status": status},
                    headers={"X-Internal-Token": settings.internal_api_token},
                    timeout=10.0,
                )
            except Exception:
                logger.error(
                    "Failed to update device %s status to %s", device_id, status, exc_info=True
                )

        await asyncio.gather(*[update_one(did) for did in device_ids])


async def _acquire_device_locks(db: AsyncSession, device_ids: list[uuid.UUID]) -> None:
    """Acquire PostgreSQL advisory locks for each device to prevent race conditions.
    Locks are sorted to avoid deadlocks and auto-release on transaction commit.
    No-op on SQLite (tests).
    """
    dialect = db.bind.dialect.name if db.bind else ""
    if dialect != "postgresql":
        return
    for device_id_str in sorted(str(d) for d in device_ids):
        lock_key = int(hashlib.sha256(device_id_str.encode()).hexdigest()[:15], 16)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


async def create_reservation(
    db: AsyncSession,
    data: ReservationCreate,
    user_id: uuid.UUID,
    token: str,
    nats_conn=None,
) -> Reservation:
    # 1. Fetch all devices from inventory (concurrently)
    try:
        devices = await _fetch_devices(data.device_ids, token)
    except ValueError as exc:
        raise exc
    except Exception as exc:
        raise RuntimeError(f"Failed to contact inventory service: {exc}") from exc

    # 2. Validate topology_type uniformity
    topology_types = {d["topology_type"] for d in devices}
    if len(topology_types) > 1:
        raise ValueError(
            f"All devices must share the same topology type. Found: {', '.join(topology_types)}"
        )
    topology_type = TopologyType(topology_types.pop())

    # 3. Check availability (status == AVAILABLE)
    unavailable = [d["name"] for d in devices if d["status"] != "AVAILABLE"]
    if unavailable:
        raise ValueError(
            f"The following devices are not available: {', '.join(unavailable)}"
        )

    # 4. Acquire advisory locks to prevent concurrent conflicting reservations
    await _acquire_device_locks(db, data.device_ids)

    # 5. Check time-window conflicts
    conflicting = await _check_conflicts(db, data.device_ids, data.start_time, data.end_time)
    if conflicting:
        raise LookupError(
            f"Time conflict: devices {[str(d) for d in conflicting]} already reserved "
            f"in the requested window"
        )

    # 6. Create reservation: store device_ids as JSON-serializable strings
    reservation = Reservation(
        user_id=user_id,
        device_ids=[str(d) for d in data.device_ids],
        topology_type=topology_type,
        purpose=data.purpose,
        start_time=data.start_time,
        end_time=data.end_time,
        status=ReservationStatus.ACTIVE,
    )
    db.add(reservation)
    await db.commit()
    await db.refresh(reservation)

    # 7. Mark devices as RESERVED in inventory (best-effort)
    await _update_device_statuses(data.device_ids, "RESERVED", token)

    # 8. Emit NATS event
    await _publish_nats_event(
        nats_conn,
        {
            "event": "reservation.created",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
            "device_ids": [str(d) for d in data.device_ids],
            "topology_type": topology_type.value,
            "start_time": data.start_time.isoformat(),
            "end_time": data.end_time.isoformat(),
        },
    )

    return reservation


async def list_user_reservations(db: AsyncSession, user_id: uuid.UUID) -> list[Reservation]:
    result = await db.execute(
        select(Reservation)
        .where(Reservation.user_id == user_id)
        .order_by(Reservation.created_at.desc())
    )
    return list(result.scalars().all())


async def get_reservation(
    db: AsyncSession, reservation_id: uuid.UUID, user_id: uuid.UUID
) -> Reservation | None:
    result = await db.execute(
        select(Reservation).where(
            Reservation.id == reservation_id,
            Reservation.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def cancel_reservation(
    db: AsyncSession, reservation_id: uuid.UUID, user_id: uuid.UUID, token: str = ""
) -> Reservation | None:
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        return None
    if reservation.status in (ReservationStatus.COMPLETED, ReservationStatus.CANCELLED):
        return reservation
    reservation.status = ReservationStatus.CANCELLED
    await db.commit()
    await db.refresh(reservation)

    # Mark devices as AVAILABLE in inventory (best-effort)
    device_ids = [uuid.UUID(d) for d in reservation.device_ids]
    await _update_device_statuses(device_ids, "AVAILABLE", token)

    return reservation


async def release_reservation(
    db: AsyncSession, reservation_id: uuid.UUID, user_id: uuid.UUID, token: str = ""
) -> Reservation | None:
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        return None
    if reservation.status != ReservationStatus.ACTIVE:
        return reservation
    reservation.status = ReservationStatus.COMPLETED
    await db.commit()
    await db.refresh(reservation)

    # Mark devices as AVAILABLE in inventory (best-effort)
    device_ids = [uuid.UUID(d) for d in reservation.device_ids]
    await _update_device_statuses(device_ids, "AVAILABLE", token)

    return reservation
