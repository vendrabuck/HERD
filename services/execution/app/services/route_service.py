"""L3 route assignment service.

Pins the route set applied to each L3 switch when a reservation is
provisioned, so deprovisioning removes exactly the set that was applied.
Routes come from the switch's latest inventory config version at provision
time (issue #20); the pinned copy in route_assignments is the source of truth
from that moment on. Deprovision NEVER re-derives from the config: a config
edited mid-reservation would otherwise remove the wrong routes or leak the
old ones.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.route_assignment import RouteAssignment

logger = logging.getLogger(__name__)

# Bound on retries when a concurrent insert trips the partial-unique index.
# Unlike VLANs there is no pool to recompute; the only realistic contender is a
# concurrent delivery of the same event for the same (reservation, device), and
# that resolves on the next iteration's idempotency read.
_MAX_ASSIGN_RETRIES = 3


async def assign_routes(
    db: AsyncSession,
    reservation_id: str,
    device_id: str,
    routes: list[dict],
) -> list[dict]:
    """Pin the route set for one L3 switch in one reservation.

    Idempotency first: an existing ACTIVE assignment for this
    (reservation, device) returns its STORED routes, ignoring the `routes`
    argument. A NATS redelivery therefore always provisions the ORIGINAL set,
    even if the switch's config was edited between deliveries.

    Concurrency: the partial-unique index on (reservation_id, device_id)
    WHERE status='ACTIVE' makes the database the arbiter. A racing duplicate
    insert raises IntegrityError; we roll back and the next iteration's
    idempotency check returns the winner's row.
    """
    res_uuid = uuid.UUID(reservation_id)
    dev_uuid = uuid.UUID(device_id)

    for _attempt in range(_MAX_ASSIGN_RETRIES):
        existing = await db.execute(
            select(RouteAssignment).where(
                RouteAssignment.reservation_id == res_uuid,
                RouteAssignment.device_id == dev_uuid,
                RouteAssignment.status == "ACTIVE",
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row.routes

        assignment = RouteAssignment(
            reservation_id=res_uuid,
            device_id=dev_uuid,
            routes=routes,
            status="ACTIVE",
        )
        db.add(assignment)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent delivery pinned this (reservation, device) first.
            # Roll back and return its stored set on the next iteration.
            await db.rollback()
            continue
        await db.refresh(assignment)

        logger.info(
            "Pinned %d route(s) for L3 switch %s in reservation %s",
            len(routes),
            device_id,
            reservation_id,
        )
        return assignment.routes

    raise RuntimeError(
        f"Could not pin routes for device {device_id} in reservation {reservation_id} "
        f"after {_MAX_ASSIGN_RETRIES} attempts due to persistent contention"
    )


async def get_pinned_routes(
    db: AsyncSession,
    reservation_id: str,
    device_id: str,
) -> list[dict] | None:
    """Return the pinned route set for one switch, or None when nothing is pinned.

    Read-only companion to assign_routes: the provision path checks this first
    so a redelivery reuses the original pinned set without consulting the
    (possibly edited) config at all.
    """
    result = await db.execute(
        select(RouteAssignment).where(
            RouteAssignment.reservation_id == uuid.UUID(reservation_id),
            RouteAssignment.device_id == uuid.UUID(device_id),
            RouteAssignment.status == "ACTIVE",
        )
    )
    row = result.scalar_one_or_none()
    return None if row is None else row.routes


async def get_route_assignments(
    db: AsyncSession,
    reservation_id: str,
) -> list[RouteAssignment]:
    """Look up active route assignments for a reservation without mutating them.

    The deprovision path reads first and releases only AFTER driver execution:
    unlike VLANs there is no re-derive fallback, so flipping status before the
    driver ran would strand routes if a transient upstream error NAKed the
    event mid-way (the redelivery would find nothing ACTIVE and no-op).
    """
    result = await db.execute(
        select(RouteAssignment).where(
            RouteAssignment.reservation_id == uuid.UUID(reservation_id),
            RouteAssignment.status == "ACTIVE",
        )
    )
    return list(result.scalars().all())


async def release_routes(
    db: AsyncSession,
    reservation_id: str,
) -> list[RouteAssignment]:
    """Release all active route assignments for a reservation.

    Returns the released assignments so the caller knows the exact routes to
    remove on each switch. An empty result means nothing was provisioned (or
    a redelivery already released them): the caller logs and no-ops.
    """
    result = await db.execute(
        select(RouteAssignment).where(
            RouteAssignment.reservation_id == uuid.UUID(reservation_id),
            RouteAssignment.status == "ACTIVE",
        )
    )
    assignments = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    for a in assignments:
        a.status = "RELEASED"
        a.released_at = now

    if assignments:
        await db.commit()
        logger.info(
            "Released %d route assignment(s) for reservation %s",
            len(assignments),
            reservation_id,
        )

    return assignments


async def release_routes_for_device(
    db: AsyncSession,
    reservation_id: str,
    device_id: str,
) -> list[RouteAssignment]:
    """Release the active route assignment for one switch in a reservation.

    Used by the reservation.updated removal path, where only the switches no
    longer adjacent to any remaining reserved device are deprovisioned.
    """
    result = await db.execute(
        select(RouteAssignment).where(
            RouteAssignment.reservation_id == uuid.UUID(reservation_id),
            RouteAssignment.device_id == uuid.UUID(device_id),
            RouteAssignment.status == "ACTIVE",
        )
    )
    assignments = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    for a in assignments:
        a.status = "RELEASED"
        a.released_at = now

    if assignments:
        await db.commit()
        logger.info(
            "Released route assignment for L3 switch %s in reservation %s",
            device_id,
            reservation_id,
        )

    return assignments
