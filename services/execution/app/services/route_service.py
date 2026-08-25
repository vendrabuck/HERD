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
from app.services.l1_assignment_service import get_wiring_state

logger = logging.getLogger(__name__)

# The L3 analogue of l1_assignment_service.FROZEN_BUILD_PENDING_RELEASE (issue #461):
# a clean route provision whose reservation froze mid-flight is parked FAILED
# intended RELEASED under this reason so the release-direction retry channels drive
# the remove_route pass. Deliberately outside the non-retryable prefixes.
FROZEN_PROVISION_PENDING_REMOVAL = "routes landed after wiring freeze; pending removal"


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


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


# --- ADR 0009 phase 5: L3 layered reconcile ledger (issue #416) --------------
#
# The route_assignments row is the per-(reservation, L3 switch) pinned route set.
# Phase 5 makes provision/deprovision fork-adjacency-driven and result-gated: the
# pin CONTENT stays exactly what issue #20 pinned (the switch's latest config
# version at provision time, captured once and reused verbatim forever, never
# re-derived), while the row STATUS now reflects the driver outcome. A failed
# provision lands FAILED intended ACTIVE; a failed removal lands FAILED intended
# RELEASED; both retry channels reattempt each FAILED row in its own direction
# (issue #369). These helpers are the per-switch analogue of l2_membership_service.


async def record_route_active(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    device_id: uuid.UUID | str,
    routes: list[dict],
) -> RouteAssignment | None:
    """Pin an L3 switch ACTIVE after a clean provision (all pinned routes configured).

    Idempotent under redelivery, mirroring record_l2_membership_active: an existing
    ACTIVE row for (reservation, device) short-circuits and returns it, and a concurrent
    provision that pinned the switch first trips the active-unique index, whereupon we
    roll back and return the winner's row. A prior non-RELEASED FAILED row for this
    reservation's switch is flipped ACTIVE in place (the pinned `routes` it already
    carried are the immutable set; we keep them and ignore the `routes` argument on the
    flip, so a redelivery cannot re-pin an edited config).

    Mirrors record_l1_connect's record-time freeze re-check (issue #461, the #412
    discipline): a provision whose reservation froze while the driver call was in
    flight must not be recorded ACTIVE (the terminal teardown has already snapshotted
    the ledger), so it is parked FAILED intended RELEASED
    (FROZEN_PROVISION_PENDING_REMOVAL) for the release-direction retry channels, which
    run while frozen and drive the route removal with the pinned set.
    """
    res_uuid = _as_uuid(reservation_id)
    dev_uuid = _as_uuid(device_id)

    existing = (
        await db.execute(
            select(RouteAssignment).where(
                RouteAssignment.reservation_id == res_uuid,
                RouteAssignment.device_id == dev_uuid,
                RouteAssignment.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    state = await get_wiring_state(db, res_uuid)
    if state is not None and state.frozen:
        row = (
            (
                await db.execute(
                    select(RouteAssignment).where(
                        RouteAssignment.reservation_id == res_uuid,
                        RouteAssignment.device_id == dev_uuid,
                        RouteAssignment.status != "RELEASED",
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = RouteAssignment(
                reservation_id=res_uuid,
                device_id=dev_uuid,
                routes=routes,
                status="FAILED",
                intended="RELEASED",
                attempts=0,
                last_error=FROZEN_PROVISION_PENDING_REMOVAL,
            )
            db.add(row)
        else:
            # Keep the row's existing pinned routes (the immutable set) on a flip.
            row.status = "FAILED"
            row.intended = "RELEASED"
            row.last_error = FROZEN_PROVISION_PENDING_REMOVAL
        await db.commit()
        await db.refresh(row)
        logger.warning(
            "Route provision for frozen reservation %s landed on switch %s; "
            "parked FAILED intended RELEASED for the release retry channel",
            res_uuid,
            dev_uuid,
        )
        return row

    reusable = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == res_uuid,
                    RouteAssignment.device_id == dev_uuid,
                    RouteAssignment.status == "FAILED",
                )
            )
        )
        .scalars()
        .first()
    )
    if reusable is not None:
        reusable.status = "ACTIVE"
        reusable.intended = "ACTIVE"
        reusable.last_error = None
        reusable.released_at = None
        await db.commit()
        await db.refresh(reusable)
        logger.info(
            "Reattempt pinned L3 switch %s ACTIVE for reservation %s",
            dev_uuid,
            res_uuid,
        )
        return reusable

    row = RouteAssignment(
        reservation_id=res_uuid,
        device_id=dev_uuid,
        routes=routes,
        status="ACTIVE",
        intended="ACTIVE",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == res_uuid,
                    RouteAssignment.device_id == dev_uuid,
                    RouteAssignment.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
    await db.refresh(row)
    logger.info(
        "Pinned %d route(s) ACTIVE for L3 switch %s in reservation %s",
        len(routes),
        dev_uuid,
        res_uuid,
    )
    return row


async def record_route_failed(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    device_id: uuid.UUID | str,
    routes: list[dict] | None,
    attempts: int,
    last_error: str | None,
    *,
    intended: str,
) -> RouteAssignment:
    """Write (or update) a FAILED route pin for a switch whose provision/removal failed.

    `intended` (issue #369) is the direction THIS write was attempting: "ACTIVE" for a
    failed provision (routes could not be configured), "RELEASED" for a failed removal
    (routes could not be removed). The retry channels split FAILED rows on it. Upserts on
    (reservation, device) for any non-RELEASED row; `attempts` ACCUMULATES.

    The #412 invariant (ADR 0009 "What does not change") extends here: an ACTIVE row is
    immutable to a stale BUILD-direction failure writer. A racing writer that proved this
    switch's routes ACTIVE after our failed build began must never be downgraded. This
    does NOT extend to a RELEASE-direction failure: an ACTIVE row there is the normal
    starting state of a removal that failed (the routes are still genuinely installed),
    and recording it FAILED is the point of issue #369. `routes` is stored so the pinned
    set survives the failure and the retry re-drives it verbatim; None keeps the row's
    existing pin.
    """
    res_uuid = _as_uuid(reservation_id)
    dev_uuid = _as_uuid(device_id)

    row = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == res_uuid,
                    RouteAssignment.device_id == dev_uuid,
                    RouteAssignment.status != "RELEASED",
                )
            )
        )
        .scalars()
        .first()
    )
    if row is not None:
        if row.status == "ACTIVE" and intended == "ACTIVE":
            logger.warning(
                "Ignoring stale L3 build failure for switch %s, reservation %s: "
                "row is ACTIVE (a concurrent writer won)",
                dev_uuid,
                res_uuid,
            )
            return row
        row.status = "FAILED"
        row.intended = intended
        row.attempts = (row.attempts or 0) + attempts
        row.last_error = last_error
        if routes is not None:
            row.routes = routes
        await db.commit()
        await db.refresh(row)
        return row

    row = RouteAssignment(
        reservation_id=res_uuid,
        device_id=dev_uuid,
        routes=routes if routes is not None else [],
        status="FAILED",
        intended=intended,
        attempts=attempts,
        last_error=last_error,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def release_route_membership(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    device_id: uuid.UUID | str,
) -> RouteAssignment | None:
    """Flip this reservation's non-RELEASED pin for a switch to RELEASED after clean removal.

    Matches status ACTIVE or FAILED (excludes RELEASED): a FAILED row here is a removal
    attempt that did not confirm success (intended RELEASED), reattempted through the
    retry channel; a successful reattempt flips it RELEASED in place. The release-side
    mirror of record_route_active's reusable-FAILED flip, and the adjacency-aware release
    the phase-5 reconcile drives when a switch leaves the intended set (mirror of
    release_l2_membership).
    """
    res_uuid = _as_uuid(reservation_id)
    dev_uuid = _as_uuid(device_id)

    row = (
        (
            await db.execute(
                select(RouteAssignment).where(
                    RouteAssignment.reservation_id == res_uuid,
                    RouteAssignment.device_id == dev_uuid,
                    RouteAssignment.status != "RELEASED",
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    row.status = "RELEASED"
    row.intended = "RELEASED"
    row.last_error = None
    row.released_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "Released route pin for L3 switch %s in reservation %s (adjacency ended)",
        dev_uuid,
        res_uuid,
    )
    return row


async def park_stale_route_build(
    db: AsyncSession,
    assignment_id: uuid.UUID,
    reason: str,
) -> RouteAssignment | None:
    """Flip a FAILED intended-ACTIVE route pin to intended RELEASED: its intent is gone.

    Issue #491, the park_stale_l1_build mirror for L3: the switch this provision was
    targeting is no longer in cabling's intended adjacency set, so the provision must
    never be reattempted. The row stays FAILED, flips to intended RELEASED (which
    route_needs_remove admits) and the release-direction channels drive remove_route for
    the PINNED set verbatim (issue #20), settling whatever the failed provision may have
    half-applied. attempts RESET to 0 (the #479 rationale: the release direction has not
    failed yet). A row no longer FAILED is left untouched (ACTIVE: a concurrent writer
    won, the #412 discipline; RELEASED: already settled).
    """
    result = await db.execute(select(RouteAssignment).where(RouteAssignment.id == assignment_id))
    row = result.scalar_one_or_none()
    if row is None or row.status != "FAILED":
        return row
    row.intended = "RELEASED"
    row.attempts = 0
    row.last_error = reason
    await db.commit()
    logger.warning(
        "Build intent gone for L3 route pin %s (switch %s, reservation %s); "
        "parked FAILED intended RELEASED for the release-direction channels",
        row.id,
        row.device_id,
        row.reservation_id,
    )
    return row


async def route_needs_remove(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    device_id: uuid.UUID | str,
) -> bool:
    """True when this switch's routes are believed still installed and a removal belongs.

    The idempotency gate for a deprovision (mirror of membership_needs_remove): true for an
    ACTIVE pin, and also for a FAILED row whose intended is RELEASED (a removal that never
    confirmed, so routes may still be installed). A FAILED row whose intended is ACTIVE (a
    provision that never applied) is excluded: there is nothing installed to remove.
    """
    res_uuid = _as_uuid(reservation_id)
    dev_uuid = _as_uuid(device_id)
    result = await db.execute(
        select(RouteAssignment.status, RouteAssignment.intended).where(
            RouteAssignment.reservation_id == res_uuid,
            RouteAssignment.device_id == dev_uuid,
            RouteAssignment.status != "RELEASED",
        )
    )
    row = result.first()
    if row is None:
        return False
    status, intended = row
    return status == "ACTIVE" or (status == "FAILED" and intended == "RELEASED")


async def get_effective_pinned_routes(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    device_id: uuid.UUID | str,
) -> list[dict] | None:
    """Return the pinned route set of this reservation's non-RELEASED pin, ACTIVE or FAILED.

    The provision path calls this FIRST so a redelivery or a retry re-provisions the
    ORIGINAL pinned set, never a re-derived one (issue #20), even when the prior attempt
    left the row FAILED rather than ACTIVE. Returns None only when no non-RELEASED row
    exists (a genuinely fresh provision), whereupon the caller reads the switch's latest
    config version to establish the pin.
    """
    res_uuid = _as_uuid(reservation_id)
    dev_uuid = _as_uuid(device_id)
    row = (
        (
            await db.execute(
                select(RouteAssignment)
                .where(
                    RouteAssignment.reservation_id == res_uuid,
                    RouteAssignment.device_id == dev_uuid,
                    RouteAssignment.status != "RELEASED",
                )
                .order_by(RouteAssignment.status.desc())  # ACTIVE before FAILED
            )
        )
        .scalars()
        .first()
    )
    return None if row is None else row.routes


async def all_route_assignments_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[RouteAssignment]:
    """Return every route pin for a reservation (all statuses), oldest first."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(RouteAssignment)
        .where(RouteAssignment.reservation_id == res_uuid)
        .order_by(RouteAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def failed_route_assignments_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[RouteAssignment]:
    """Return this reservation's FAILED route pins, oldest first (retry candidates)."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(RouteAssignment)
        .where(
            RouteAssignment.reservation_id == res_uuid,
            RouteAssignment.status == "FAILED",
        )
        .order_by(RouteAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def due_failed_route_rows(
    db: AsyncSession,
    limit: int,
    max_attempts: int,
) -> list[RouteAssignment]:
    """FAILED route pins still under the attempts cap, oldest first, batch-capped.

    The background auto-retry channel's per-tick L3 candidate set, the due_failed_rows
    analogue. Retryability and the frozen guard are applied by the caller.
    """
    result = await db.execute(
        select(RouteAssignment)
        .where(
            RouteAssignment.status == "FAILED",
            RouteAssignment.attempts < max_attempts,
        )
        .order_by(RouteAssignment.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())
