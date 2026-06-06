"""Fabric-aware VLAN assignment service.

Assigns VLAN IDs to reservations, ensuring no two active reservations in the
same L2 fabric share the same VLAN ID.  Reservations in isolated fabrics
(no physical path between their L2 switches) may safely reuse VLAN IDs.
"""

import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.vlan_assignment import VlanAssignment

logger = logging.getLogger(__name__)

VLAN_MIN = 2
VLAN_MAX = 4094

# Bound on retries when a concurrent insert trips the partial-unique index. Each
# retry recomputes the free VLAN against the updated in-use set, so a small cap
# is enough to absorb realistic contention; exhaustion signals a real anomaly.
_MAX_ASSIGN_RETRIES = 5


def _derive_vlan_id(reservation_id: str) -> int:
    """Derive a preferred VLAN ID (2-4094) from a reservation UUID."""
    return (uuid.UUID(reservation_id).int % (VLAN_MAX - VLAN_MIN + 1)) + VLAN_MIN


async def fetch_fabric_id(device_id: str) -> uuid.UUID | None:
    """Fetch the fabric ID for a device from the cabling service."""
    url = f"{settings.cabling_service_url}/fabric/internal"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"device_id": device_id},
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Failed to fetch fabric for device %s: %s", device_id, resp.status_code
                )
                return None
            return uuid.UUID(resp.json()["fabric_id"])
    except Exception:
        logger.error("Error fetching fabric for device %s", device_id, exc_info=True)
        return None


async def find_or_assign_vlan(
    db: AsyncSession,
    reservation_id: str,
    fabric_id: uuid.UUID,
    switch_device_ids: list[str],
) -> int:
    """Assign a conflict-free VLAN ID for a reservation within a fabric.

    1. If an ACTIVE assignment already exists for this (reservation, fabric), return it.
    2. Try the preferred VLAN derived from the reservation UUID.
    3. If that VLAN is in use in this fabric, find the lowest free VLAN.
    4. Insert a VlanAssignment row and return the assigned VLAN ID.

    Concurrency: the per-fabric partial-unique index on (fabric_id, vlan_id) WHERE
    status='ACTIVE' makes the database the arbiter. If a concurrent caller claims the
    same VLAN between our read and our insert, the commit raises IntegrityError; we
    roll back, recompute against the now-updated in-use set, and retry. A racing
    duplicate for THIS reservation (e.g. redelivered NATS event) resolves on the next
    iteration's idempotency check, which returns the already-committed VLAN.
    """
    preferred = _derive_vlan_id(reservation_id)

    for _attempt in range(_MAX_ASSIGN_RETRIES):
        # Check for existing assignment (idempotency for retries and redelivery)
        existing = await db.execute(
            select(VlanAssignment).where(
                VlanAssignment.reservation_id == uuid.UUID(reservation_id),
                VlanAssignment.fabric_id == fabric_id,
                VlanAssignment.status == "ACTIVE",
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row.vlan_id

        # Get VLANs currently in use in this fabric
        used_result = await db.execute(
            select(VlanAssignment.vlan_id).where(
                VlanAssignment.fabric_id == fabric_id,
                VlanAssignment.status == "ACTIVE",
            )
        )
        used_vlans = {r[0] for r in used_result.all()}

        # Try preferred VLAN first, else the lowest free VLAN in range
        if preferred not in used_vlans:
            vlan_id = preferred
        else:
            vlan_id = None
            for candidate in range(VLAN_MIN, VLAN_MAX + 1):
                if candidate not in used_vlans:
                    vlan_id = candidate
                    break
            if vlan_id is None:
                raise RuntimeError(
                    f"No free VLAN IDs in fabric {fabric_id} (all {VLAN_MAX - VLAN_MIN + 1} in use)"
                )

        assignment = VlanAssignment(
            reservation_id=uuid.UUID(reservation_id),
            fabric_id=fabric_id,
            vlan_id=vlan_id,
            switch_device_ids=[str(sid) for sid in switch_device_ids],
            status="ACTIVE",
        )
        db.add(assignment)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent caller took this VLAN (or this reservation) first. Roll
            # back and retry: the loop re-reads the in-use set and either picks a
            # different VLAN or returns the now-existing assignment.
            await db.rollback()
            continue
        await db.refresh(assignment)

        logger.info(
            "Assigned VLAN %d to reservation %s in fabric %s (preferred was %d)",
            vlan_id,
            reservation_id,
            fabric_id,
            preferred,
        )
        return vlan_id

    raise RuntimeError(
        f"Could not assign a VLAN in fabric {fabric_id} after {_MAX_ASSIGN_RETRIES} "
        "attempts due to persistent contention"
    )


async def release_vlan(
    db: AsyncSession,
    reservation_id: str,
) -> list[VlanAssignment]:
    """Release all active VLAN assignments for a reservation.

    Returns the list of released assignments so the caller knows
    the actual VLAN IDs to deprovision on each fabric's switches.
    """
    result = await db.execute(
        select(VlanAssignment).where(
            VlanAssignment.reservation_id == uuid.UUID(reservation_id),
            VlanAssignment.status == "ACTIVE",
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
            "Released %d VLAN assignment(s) for reservation %s",
            len(assignments),
            reservation_id,
        )

    return assignments


async def get_vlan_assignments(
    db: AsyncSession,
    reservation_id: str,
) -> list[VlanAssignment]:
    """Look up active VLAN assignments for a reservation."""
    result = await db.execute(
        select(VlanAssignment).where(
            VlanAssignment.reservation_id == uuid.UUID(reservation_id),
            VlanAssignment.status == "ACTIVE",
        )
    )
    return list(result.scalars().all())
