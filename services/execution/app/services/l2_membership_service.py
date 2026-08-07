"""L2 VLAN port-membership applied-state projection (ADR 0009 Decision 2, issue #416).

The L2 analogue of l1_assignment_service. Phase 4 makes VLAN membership
connection-driven: a reservation.wiring_changed reconcile derives the intended
per-port memberships from the fork's recorded hops (option C, layer-agnostic) and
drives add_to_vlan / remove_from_vlan against this ledger, exactly as the L1 path
drives connect_ports / disconnect_ports against l1_connection_assignments.

Membership is split from allocation (Decision 2): vlan_assignments stays the
per-fabric VLAN-NUMBER allocation (find_or_assign, written before any driver call
since allocation is a database decision, not a hardware outcome); this ledger
records DRIVER-CONFIRMED per-port membership in that allocation. `status` reflects
driver outcomes only and is never written ACTIVE before the hardware confirms,
closing the gap where a vlan_assignments row alone claimed a VLAN the hardware
never took.

`intended` mirrors l1_connection_assignments.intended (issue #369): the direction
(ACTIVE for join, RELEASED for leave) the row's last write was attempting, so the
retry channels reattempt each FAILED row in its own direction.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.l2_port_assignment import L2PortAssignment
from app.services.l1_assignment_service import get_wiring_state

logger = logging.getLogger(__name__)

# The L2 analogue of l1_assignment_service.FROZEN_BUILD_PENDING_RELEASE (issue #461):
# a successful add_to_vlan whose reservation froze mid-flight is parked FAILED
# intended RELEASED under this reason so the release-direction retry channels drive
# the remove_from_vlan. Deliberately outside the non-retryable prefixes.
FROZEN_JOIN_PENDING_REMOVAL = "join landed after wiring freeze; pending removal"

# Issue #479: a FROZEN reservation's stale ACTIVE membership discovered at record time
# is parked under this reason. Unlike L1's record-time supersession (issue #461), the
# row is NOT flipped straight to RELEASED: a successful add_to_vlan proves nothing
# about whether the stale membership left the hardware (memberships can coexist on a
# port), so the release-direction retry channels own the settlement, usually via the
# supersede_l2_release_if_reclaimed no-driver path once the superseding reservation's
# ACTIVE row exists; the retry pass then releases the settled rows' orphaned
# allocations. Deliberately outside the non-retryable prefixes.
STALE_JOIN_SUPERSEDED_PENDING_REMOVAL = (
    "superseded at record time by another reservation's membership; pending removal"
)


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


async def _find_active(
    db: AsyncSession, switch_uuid: uuid.UUID, port: str, vlan_assignment_id: uuid.UUID
) -> L2PortAssignment | None:
    result = await db.execute(
        select(L2PortAssignment).where(
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.vlan_assignment_id == vlan_assignment_id,
            L2PortAssignment.status == "ACTIVE",
        )
    )
    return result.scalar_one_or_none()


async def _find_reusable_failed(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    switch_uuid: uuid.UUID,
    port: str,
) -> L2PortAssignment | None:
    """This reservation's non-RELEASED (FAILED) membership row for a port, if any.

    The candidate a successful (re)join flips ACTIVE in place. RELEASED rows are
    excluded so a torn-down membership is never resurrected. Keyed on (reservation,
    switch, port), NOT vlan_assignment_id: a re-save can reallocate the fabric's VLAN
    number, so the failed row's stored allocation may differ from the current one; the
    membership identity a reservation owns is the physical (switch, port).
    """
    result = await db.execute(
        select(L2PortAssignment).where(
            L2PortAssignment.reservation_id == reservation_id,
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.status == "FAILED",
        )
    )
    return result.scalars().first()


async def record_l2_membership_active(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    vlan_assignment_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port: str,
) -> L2PortAssignment | None:
    """Insert an ACTIVE membership after a successful add_to_vlan.

    Idempotent under redelivery: an existing ACTIVE row for the same (switch, port,
    allocation) short-circuits, and a concurrent join that claims the port first trips
    the active-unique index, whereupon we roll back and return the winner's row. A prior
    FAILED row for this reservation's (switch, port) is flipped ACTIVE in place rather
    than duplicated. Mirrors record_l1_connect, including its record-time freeze
    re-check (issue #461, the #412 discipline): a join whose reservation froze while
    the driver call was in flight must not be recorded ACTIVE (the terminal teardown
    has already snapshotted the ledger), so it is parked FAILED intended RELEASED
    (FROZEN_JOIN_PENDING_REMOVAL) for the release-direction retry channels, which run
    while frozen and drive the remove_from_vlan.
    """
    res_uuid = _as_uuid(reservation_id)
    vlan_uuid = _as_uuid(vlan_assignment_id)
    switch_uuid = _as_uuid(switch_device_id)

    existing = await _find_active(db, switch_uuid, port, vlan_uuid)
    if existing is not None:
        return existing

    # Issue #479: another reservation's ACTIVE membership on this (switch, port)
    # whose reservation's wiring is FROZEN is provably stale: the freeze lands and
    # commits before any teardown driver call (issue #461), so a frozen row is by
    # definition condemned wiring an interrupted teardown left behind. It is parked
    # FAILED intended RELEASED for the release-direction channels rather than
    # flipped RELEASED (the L1 record-time rule, whose physics does not transfer:
    # connect_ports displaces the old cross-connect, add_to_vlan displaces
    # nothing, so the membership may still exist on the hardware). Without this
    # the stale row is invisible to every convergence mechanism (retry selects
    # FAILED rows only) and misreports wiring status for its dead reservation.
    #
    # The frozen gate is load-bearing, not an optimization: an UNFROZEN
    # cross-reservation ACTIVE row can belong to the port's rightful LIVE holder
    # (this join may itself be a stale-intent build reattempt, see the issue #491
    # flaw), and port-claim exclusivity in cabling proves nothing about which
    # ledger writer is current. Those rows are left alone (the pre-#479
    # release-side FAILED-row guard covers what it covered before; an unfrozen
    # stale row left ACTIVE remains invisible, the pre-#479 status quo, traded
    # deliberately for soundness).
    # Own-reservation rows are also out of scope: the owning reconcile's remove
    # pass converges those.
    stale_result = await db.execute(
        select(L2PortAssignment).where(
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.reservation_id != res_uuid,
            L2PortAssignment.status == "ACTIVE",
        )
    )
    for stale in stale_result.scalars().all():
        stale_state = await get_wiring_state(db, stale.reservation_id)
        if stale_state is None or not stale_state.frozen:
            logger.warning(
                "Cross-reservation ACTIVE L2 membership of reservation %s on "
                "switch %s port %s is not frozen; leaving it untouched while "
                "reservation %s records a join on the same port",
                stale.reservation_id,
                switch_uuid,
                port,
                res_uuid,
            )
            continue
        stale.status = "FAILED"
        stale.intended = "RELEASED"
        # Fresh budget: prior attempts counted the build direction, and the usual
        # settlement (the #424 supersession) needs one tick, not a driver call.
        stale.attempts = 0
        stale.last_error = STALE_JOIN_SUPERSEDED_PENDING_REMOVAL
        logger.warning(
            "Parking stale ACTIVE L2 membership of frozen reservation %s on "
            "switch %s port %s FAILED intended RELEASED: reservation %s "
            "recorded a successful join on the port",
            stale.reservation_id,
            switch_uuid,
            port,
            res_uuid,
        )
    # Unlike record_l1_connect there is no flush-before-insert concern: parked rows
    # always differ from the new row in vlan_assignment_id (allocations are
    # per-reservation), so the partial-unique index cannot collide.

    state = await get_wiring_state(db, res_uuid)
    if state is not None and state.frozen:
        row = await _find_reusable_failed(db, res_uuid, switch_uuid, port)
        if row is None:
            row = L2PortAssignment(
                reservation_id=res_uuid,
                vlan_assignment_id=vlan_uuid,
                switch_device_id=switch_uuid,
                port=port,
                status="FAILED",
                intended="RELEASED",
                attempts=0,
                last_error=FROZEN_JOIN_PENDING_REMOVAL,
            )
            db.add(row)
        else:
            row.status = "FAILED"
            row.intended = "RELEASED"
            # Overwrite the allocation with the one the successful join actually used,
            # so the pending remove drives the real fabric VLAN.
            row.vlan_assignment_id = vlan_uuid
            row.last_error = FROZEN_JOIN_PENDING_REMOVAL
        await db.commit()
        await db.refresh(row)
        logger.warning(
            "Join for frozen reservation %s landed on switch %s port %s; "
            "parked FAILED intended RELEASED for the release retry channel",
            res_uuid,
            switch_uuid,
            port,
        )
        return row

    reusable = await _find_reusable_failed(db, res_uuid, switch_uuid, port)
    if reusable is not None:
        reusable.status = "ACTIVE"
        reusable.intended = "ACTIVE"
        reusable.vlan_assignment_id = vlan_uuid
        reusable.last_error = None
        reusable.released_at = None
        await db.commit()
        await db.refresh(reusable)
        logger.info(
            "Reattempt flipped FAILED to ACTIVE for switch %s port %s, reservation %s",
            switch_uuid,
            port,
            res_uuid,
        )
        return reusable

    row = L2PortAssignment(
        reservation_id=res_uuid,
        vlan_assignment_id=vlan_uuid,
        switch_device_id=switch_uuid,
        port=port,
        status="ACTIVE",
        intended="ACTIVE",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await _find_active(db, switch_uuid, port, vlan_uuid)
    await db.refresh(row)
    logger.info(
        "Recorded ACTIVE L2 membership for switch %s port %s, reservation %s",
        switch_uuid,
        port,
        res_uuid,
    )
    return row


async def release_l2_membership(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port: str,
) -> L2PortAssignment | None:
    """Flip this reservation's ACTIVE (or retried-FAILED) membership for a port to RELEASED.

    Returns the released row, or None if no matching row (already released, or never
    recorded). Keyed on (reservation, switch, port) so a teardown only releases what this
    reservation holds. Matches status ACTIVE or FAILED (excludes RELEASED): a FAILED row
    here is a disconnect attempt that did not confirm success (intended RELEASED),
    reattempted through the retry channel; a successful reattempt flips it RELEASED in
    place. The release-side mirror of record_l2_membership_active's reusable-FAILED flip.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)

    result = await db.execute(
        select(L2PortAssignment).where(
            L2PortAssignment.reservation_id == res_uuid,
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.status != "RELEASED",
        )
    )
    row = result.scalars().first()
    if row is None:
        return None
    row.status = "RELEASED"
    row.intended = "RELEASED"
    row.last_error = None
    row.released_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "Released L2 membership for switch %s port %s, reservation %s",
        switch_uuid,
        port,
        res_uuid,
    )
    return row


async def record_l2_failed(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    vlan_assignment_id: uuid.UUID | str | None,
    switch_device_id: uuid.UUID | str,
    port: str,
    attempts: int,
    last_error: str,
    *,
    intended: str,
) -> L2PortAssignment:
    """Write (or update) a FAILED membership row for a port op that could not be applied.

    `intended` (issue #369) is the direction THIS write was attempting: "ACTIVE" for a
    failed add_to_vlan/join, "RELEASED" for a failed remove_from_vlan/leave. Every caller
    passes it explicitly so a build failure and a release failure are never conflated;
    the retry channels split FAILED rows on it. Upserts on (reservation, switch, port) for
    any non-RELEASED row so a redelivery before the version is stamped, or a reattempt,
    updates the existing row rather than duplicating; `attempts` ACCUMULATES.

    The #412 invariant (ADR 0009 "What does not change") extends here: an ACTIVE row is
    immutable to a stale BUILD-direction failure writer. A racing writer that proved this
    membership joined AFTER our failed build began must never be downgraded; attempts and
    intended stay untouched by the refused write. This does NOT extend to a
    RELEASE-direction failure: an ACTIVE row there is the normal starting state of a leave
    that failed (the port is still genuinely a member), and recording it FAILED is the
    point of issue #369. `vlan_assignment_id` may be None only when a build never resolved
    an allocation; the row keeps its prior allocation in that case.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    vlan_uuid = _as_uuid(vlan_assignment_id) if vlan_assignment_id else None

    result = await db.execute(
        select(L2PortAssignment).where(
            L2PortAssignment.reservation_id == res_uuid,
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.status != "RELEASED",
        )
    )
    row = result.scalars().first()
    if row is not None:
        if row.status == "ACTIVE" and intended == "ACTIVE":
            logger.warning(
                "Ignoring stale L2 build failure for switch %s port %s, reservation %s: "
                "row is ACTIVE (a concurrent writer won)",
                switch_uuid,
                port,
                res_uuid,
            )
            return row
        row.status = "FAILED"
        row.intended = intended
        row.attempts = (row.attempts or 0) + attempts
        row.last_error = last_error
        if vlan_uuid is not None:
            row.vlan_assignment_id = vlan_uuid
        await db.commit()
        return row

    row = L2PortAssignment(
        reservation_id=res_uuid,
        # A build that never resolved an allocation still needs a non-null column; the
        # nil UUID is a harmless placeholder for such a row (it is FAILED and drives no
        # allocation lifecycle). A resolved allocation always overwrites it.
        vlan_assignment_id=vlan_uuid if vlan_uuid is not None else uuid.UUID(int=0),
        switch_device_id=switch_uuid,
        port=port,
        status="FAILED",
        intended=intended,
        attempts=attempts,
        last_error=last_error,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def park_stale_l2_build(
    db: AsyncSession,
    assignment_id: uuid.UUID,
    reason: str,
) -> L2PortAssignment | None:
    """Flip a FAILED intended-ACTIVE membership to intended RELEASED: its intent is gone.

    Issue #491, the park_stale_l1_build mirror: the (switch, port) this join was trying
    to establish is no longer in cabling's intended membership set, so the join must
    never be reattempted. The row stays FAILED, flips to intended RELEASED (which
    membership_needs_remove admits) and the release-direction channels drive the
    idempotent remove_from_vlan against the row's recorded allocation. attempts RESET to
    0 (the #479 rationale: the release direction has not failed yet). A row no longer
    FAILED is left untouched (ACTIVE: a concurrent writer won, the #412 discipline;
    RELEASED: already settled). Callers must NOT park a nil-allocation row through here:
    with no resolvable VLAN number there is nothing for the release channel to drive, so
    such rows are settled directly via release_l2_membership instead.
    """
    result = await db.execute(select(L2PortAssignment).where(L2PortAssignment.id == assignment_id))
    row = result.scalar_one_or_none()
    if row is None or row.status != "FAILED":
        return row
    row.intended = "RELEASED"
    row.attempts = 0
    row.last_error = reason
    await db.commit()
    logger.warning(
        "Build intent gone for L2 membership %s (switch %s port %s, reservation %s); "
        "parked FAILED intended RELEASED for the release-direction channels",
        row.id,
        row.switch_device_id,
        row.port,
        row.reservation_id,
    )
    return row


async def is_membership_active(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port: str,
) -> bool:
    """True when this reservation already holds an ACTIVE membership for the port.

    The idempotency gate for a join: a build whose (switch, port) is already ACTIVE for
    this reservation is a convergent no-op, not a driver call. Mirrors is_pair_active.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    result = await db.execute(
        select(L2PortAssignment.id).where(
            L2PortAssignment.reservation_id == res_uuid,
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.status == "ACTIVE",
        )
    )
    return result.first() is not None


async def membership_needs_remove(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port: str,
) -> bool:
    """True when this membership is believed still live and a remove_from_vlan belongs.

    The idempotency gate for a leave (issue #369), the pair_needs_release analogue:
    true for an ACTIVE row, and also for a FAILED row whose intended is RELEASED (a
    leave attempt that never confirmed success, so the port may still be a member).
    A FAILED row whose intended is ACTIVE (a join that never applied) is excluded:
    there is nothing live to remove.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    result = await db.execute(
        select(L2PortAssignment.status, L2PortAssignment.intended).where(
            L2PortAssignment.reservation_id == res_uuid,
            L2PortAssignment.switch_device_id == switch_uuid,
            L2PortAssignment.port == port,
            L2PortAssignment.status != "RELEASED",
        )
    )
    row = result.first()
    if row is None:
        return False
    status, intended = row
    return status == "ACTIVE" or (status == "FAILED" and intended == "RELEASED")


async def supersede_l2_release_if_reclaimed(
    db: AsyncSession,
    row: L2PortAssignment,
) -> bool:
    """Settle a release-direction FAILED membership as RELEASED, no driver call, when
    another reservation already holds an ACTIVE membership on the same (switch, port).

    A release-direction FAILED row (intended RELEASED) is a leave that never confirmed
    success, so this reservation's ledger still believes the port may be a member. But a
    physical switch port carries one cross-connect / one occupant: if ANOTHER reservation
    now holds an ACTIVE membership on the same (switch_device_id, port), the re-wire that
    granted it already reconfigured the port, so the leave this row wants is done in
    hardware. Re-firing remove_from_vlan would at best no-op and at worst strip the newer
    reservation's live membership, so we flip the row RELEASED in place and skip the
    driver. The cross-reservation analogue of supersede_release_if_reclaimed for L1.
    Returns True when superseded (now RELEASED), False when a driver remove is warranted.
    """
    result = await db.execute(
        select(L2PortAssignment.id).where(
            L2PortAssignment.switch_device_id == row.switch_device_id,
            L2PortAssignment.port == row.port,
            L2PortAssignment.reservation_id != row.reservation_id,
            L2PortAssignment.status == "ACTIVE",
        )
    )
    if result.first() is None:
        return False
    await release_l2_membership(db, row.reservation_id, row.switch_device_id, row.port)
    return True


async def active_memberships_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[L2PortAssignment]:
    """Return this reservation's ACTIVE membership rows (the applied L2 set)."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(L2PortAssignment).where(
            L2PortAssignment.reservation_id == res_uuid,
            L2PortAssignment.status == "ACTIVE",
        )
    )
    return list(result.scalars().all())


async def all_memberships_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[L2PortAssignment]:
    """Return every membership row for a reservation (all statuses), oldest first."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(L2PortAssignment)
        .where(L2PortAssignment.reservation_id == res_uuid)
        .order_by(L2PortAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def failed_memberships_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[L2PortAssignment]:
    """Return this reservation's FAILED membership rows, oldest first (retry candidates)."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(L2PortAssignment)
        .where(
            L2PortAssignment.reservation_id == res_uuid,
            L2PortAssignment.status == "FAILED",
        )
        .order_by(L2PortAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def due_failed_l2_rows(
    db: AsyncSession,
    limit: int,
    max_attempts: int,
) -> list[L2PortAssignment]:
    """FAILED membership rows still under the attempts cap, oldest first, batch-capped.

    The background auto-retry channel's per-tick L2 candidate set, the due_failed_rows
    analogue. Retryability and the frozen guard are applied by the caller.
    """
    result = await db.execute(
        select(L2PortAssignment)
        .where(
            L2PortAssignment.status == "FAILED",
            L2PortAssignment.attempts < max_attempts,
        )
        .order_by(L2PortAssignment.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_active_memberships_for_vlan(
    db: AsyncSession,
    vlan_assignment_id: uuid.UUID | str,
) -> int:
    """Count ACTIVE membership rows referencing a vlan_assignment (allocation coupling).

    The allocation lifecycle hinge (ADR 0009 Decision 4): a fabric's VLAN-number
    allocation is released when its last ACTIVE membership leaves. The reconcile calls
    this after driving removes and adds; a zero count means the allocation is orphaned
    and the caller releases the vlan_assignments row.
    """
    vlan_uuid = _as_uuid(vlan_assignment_id)
    result = await db.execute(
        select(func.count())
        .select_from(L2PortAssignment)
        .where(
            L2PortAssignment.vlan_assignment_id == vlan_uuid,
            L2PortAssignment.status == "ACTIVE",
        )
    )
    return int(result.scalar_one())


def _row_get(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _strictly_after(a, b) -> bool:
    """True if timestamp a is strictly after b, treating None as oldest."""
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def compute_backfill_l2_memberships(runs, vlan_assignments) -> list[dict]:
    """Reconstruct currently-live L2 memberships from execution_run rows (migration 0019).

    `runs` is any iterable exposing reservation_id, device_id (the switch), action,
    status, port_a (the port), created_at. `vlan_assignments` exposes id, reservation_id,
    switch_device_ids (list), status. Dicts or attribute objects both work.

    A membership is live when its latest SUCCESS add_to_vlan for a (reservation, switch,
    port) is not followed by a SUCCESS remove_from_vlan (the compute_backfill_assignments
    pattern, applied to VLAN ports). Each live membership is mapped to the reservation's
    ACTIVE vlan_assignment whose switch_device_ids contains the switch; a membership with
    no such allocation is skipped (nothing to key it to). Returns a list of dicts with
    reservation_id, vlan_assignment_id, switch_device_id, port. Rows with missing fields
    are skipped so a malformed historical payload cannot break the migration.

    Kept in sync with the frozen copy embedded in migration 0019: migrations must not
    import live application code, so the two copies are intentionally duplicated.
    """
    latest_add: dict[tuple, object] = {}
    latest_remove: dict[tuple, object] = {}

    for r in runs:
        if _row_get(r, "status") != "SUCCESS":
            continue
        action = _row_get(r, "action")
        if action not in ("add_to_vlan", "remove_from_vlan"):
            continue
        reservation_id = _row_get(r, "reservation_id")
        switch_id = _row_get(r, "device_id")
        port = _row_get(r, "port_a")
        if reservation_id is None or switch_id is None or port is None:
            continue
        key = (reservation_id, switch_id, str(port))
        created = _row_get(r, "created_at")
        target = latest_add if action == "add_to_vlan" else latest_remove
        prev = target.get(key)
        if prev is None or _strictly_after(created, prev):
            target[key] = created

    # Build a per-reservation lookup: switch_device_id -> vlan_assignment_id, from the
    # ACTIVE allocations. A switch listed in more than one ACTIVE allocation for the same
    # reservation is not reconstructable unambiguously, so the last one wins (a benign
    # tie: both are the same reservation's VLAN, and re-add on the first heal is harmless).
    alloc_by_res_switch: dict[tuple, object] = {}
    for va in vlan_assignments:
        if _row_get(va, "status") != "ACTIVE":
            continue
        res_id = _row_get(va, "reservation_id")
        va_id = _row_get(va, "id")
        sids = _row_get(va, "switch_device_ids") or []
        for sid in sids:
            alloc_by_res_switch[(res_id, str(sid))] = va_id

    result: list[dict] = []
    for key, add_created in latest_add.items():
        remove_created = latest_remove.get(key)
        if remove_created is not None and not _strictly_after(add_created, remove_created):
            # Removed at or after the latest add: the port is no longer a member.
            continue
        reservation_id, switch_id, port = key
        va_id = alloc_by_res_switch.get((reservation_id, str(switch_id)))
        if va_id is None:
            continue
        result.append(
            {
                "reservation_id": reservation_id,
                "vlan_assignment_id": va_id,
                "switch_device_id": switch_id,
                "port": port,
            }
        )
    return result
