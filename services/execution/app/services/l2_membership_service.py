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

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.l2_port_assignment import L2PortAssignment
from app.services._uuid_utils import as_uuid as _as_uuid
from app.services.l1_assignment_service import get_wiring_state
from app.services.wiring_claim import (
    WiringRowClaims,
    claim_write_clauses,
    direction_guarded_values,
    unclaimed,
)
from app.services.wiring_due import frozen_build_failure_values, tick_would_drive

logger = logging.getLogger(__name__)

# The nil-UUID allocation placeholder of a join that never resolved an allocation.
_NIL = uuid.UUID(int=0)

# Reason the terminal teardown parks a FAILED intended-ACTIVE membership under (issue
# #1501), the route_service.TEARDOWN_PENDING_REMOVAL mirror: a join that did not
# confirm may still have landed (a timed-out driver call), so at teardown the row flips
# to intended RELEASED and is removed from its allocation's VLAN, and, should that
# fail, the release-direction retry channels finish it. Outside the non-retryable
# prefixes.
TEARDOWN_PENDING_VLAN_REMOVAL = "reservation ended; pending VLAN removal"

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

# Issue #1413: a join whose allocation is no longer ACTIVE when its membership would be
# recorded is failed under this reason, intended ACTIVE, still pointing at the released
# allocation; the build retry resolves a fresh allocation on a later pass
# (wiring_retry_service._reattempt_l2_rows). The define pre-pass parks the joins of a
# switch whose definition lost to a release under it too. Deliberately outside the
# non-retryable prefixes.
JOIN_LOST_ALLOCATION_RELEASED = (
    "VLAN allocation released before the join was recorded; pending re-allocation"
)

# The log action of a join failed under JOIN_LOST_ALLOCATION_RELEASED.
JOIN_LOST_ACTION = "l2_join_lost_to_allocation_release"


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
    *,
    row_id: uuid.UUID | None = None,
    claims: WiringRowClaims | None = None,
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

    Issue #1413: before the ACTIVE write (the FAILED flip or the insert) the allocation
    row is locked FOR SHARE while it is ACTIVE (vlan_service.hold_active_allocation),
    and the lock is held to this call's commit, so a release of the allocation either
    waits for that commit and then sees the membership, or committed first and this
    join loses. A join that loses, its allocation RELEASED or absent, is rolled back
    (the other reservations' rows parked above are not written) and failed through
    record_l2_failed: intended ACTIVE, one attempt, JOIN_LOST_ALLOCATION_RELEASED, the
    allocation it used kept on the row; one line is logged with the action
    JOIN_LOST_ACTION. `row_id` and `claims` are the retry channel's, used only for that
    failure write (the row-identity compare-and-swap of WIRE-LEDGER-8); the reconcile
    passes neither and the write is the key-based upsert. The returned row is that
    failure write's. The existing-ACTIVE short-circuit and the frozen park above take
    no lock: neither writes an ACTIVE row.
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
        # Parked for the release channel; it holds no drive claim (issue #817).
        stale.claimed_until = None
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
            # This write finalizes the row for the release channel (issue #817).
            row.claimed_until = None
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

    from app.services.vlan_service import hold_active_allocation

    if not await hold_active_allocation(db, vlan_uuid):
        await db.rollback()
        logger.warning(
            "Join for reservation %s on switch %s port %s lost to the release of VLAN "
            "allocation %s; failed for the build retry to re-allocate",
            res_uuid,
            switch_uuid,
            port,
            vlan_uuid,
            extra={
                "action": JOIN_LOST_ACTION,
                "reservation_id": str(res_uuid),
                "vlan_assignment_id": str(vlan_uuid),
            },
        )
        return await record_l2_failed(
            db,
            res_uuid,
            vlan_uuid,
            switch_uuid,
            port,
            1,
            JOIN_LOST_ALLOCATION_RELEASED,
            intended="ACTIVE",
            row_id=row_id,
            claims=claims,
        )

    reusable = await _find_reusable_failed(db, res_uuid, switch_uuid, port)
    if reusable is not None:
        reusable_id = reusable.id
        result = await db.execute(
            update(L2PortAssignment)
            .where(
                L2PortAssignment.id == reusable_id,
                L2PortAssignment.status == "FAILED",
            )
            .values(
                status="ACTIVE",
                intended="ACTIVE",
                vlan_assignment_id=vlan_uuid,
                last_error=None,
                released_at=None,
                # The row is settled ACTIVE, so it holds no drive claim (issue #817).
                claimed_until=None,
            )
        )
        if result.rowcount == 0:
            # A concurrent writer (the consumer apply and either retry channel all
            # reach this function) flipped or released the row between our SELECT and
            # this UPDATE: never overwrite the winner, re-read and return its row
            # as-is. The same SQL compare-and-swap record_route_active uses, aligned
            # here by issue #817 so all three layers share one flip discipline.
            await db.rollback()
            winner = (
                await db.execute(select(L2PortAssignment).where(L2PortAssignment.id == reusable_id))
            ).scalar_one_or_none()
            logger.info(
                "stale reattempt writer skipped for switch %s port %s, reservation %s: "
                "row already flipped by a concurrent writer",
                switch_uuid,
                port,
                res_uuid,
            )
            return winner
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
    # A settled row holds no drive claim (issue #817).
    row.claimed_until = None
    await db.commit()
    logger.info(
        "Released L2 membership for switch %s port %s, reservation %s",
        switch_uuid,
        port,
        res_uuid,
    )
    return row


async def _record_l2_failed_for_row(
    db: AsyncSession,
    row_id: uuid.UUID,
    expected_status: str,
    *,
    res_uuid: uuid.UUID,
    switch_uuid: uuid.UUID,
    port: str,
    attempts: int,
    last_error: str,
    vlan_uuid: uuid.UUID | None,
    intended: str,
    claims: WiringRowClaims | None,
) -> L2PortAssignment | None:
    """The row-identity compare-and-swap behind record_l2_failed's `row_id` path.

    See record_l2_failed's docstring for why the retry channels write by row id
    (issue #814). One atomic `UPDATE ... WHERE id AND status`, plus the claim clause
    of wiring_claim.claim_write_clauses (issue #1479), no insert branch:
    rowcount zero means a concurrent writer moved the row or another pass holds its
    drive claim, so this failure is not written and the row joins `claims.lost`.

    Issue #1496: on a row this pass does not hold (`claims` None, or the row not in
    `claims.held`), every value is written only where
    `wiring_claim.direction_admits(model, intended)` holds, in the same UPDATE: the
    row still intends the direction this failure records, or the failure is a release
    and the reservation is frozen. Such a write runs before any per-row claim (a
    switch read, template read, driver load, or login failure, an L2 create_vlan or
    re-allocation failure, or an L3 drive-gate refusal), so the row may have been
    parked since the pass planned it. The value CASE (`direction_guarded_values`) and
    `RETURNING intended` tell an admitted write from a refused one; a refused write is
    rolled back, which is what leaves every value as it was, returns None, and adds the
    row to `claims.wrong_direction`. A row this pass holds takes no compare: its claim
    compared the direction already. Known gap: with `claims` None a refused write is
    recorded nowhere and logs nothing (no retry caller passes `row_id` without
    `claims` today).
    """
    values: dict = {
        "status": "FAILED",
        "intended": intended,
        "attempts": func.coalesce(L2PortAssignment.attempts, 0) + attempts,
        "last_error": last_error,
        # The drive this failure records is over, so release the claim (issue #817).
        "claimed_until": None,
    }
    if vlan_uuid is not None:
        values["vlan_assignment_id"] = vlan_uuid
    if claims is None or row_id not in claims.held:
        values = direction_guarded_values(L2PortAssignment, values, intended)
    result = await db.execute(
        update(L2PortAssignment)
        .where(
            L2PortAssignment.id == row_id,
            L2PortAssignment.status == expected_status,
            *claim_write_clauses(L2PortAssignment, row_id, claims),
        )
        .values(**values)
        .returning(L2PortAssignment.intended)
    )
    found = result.first()
    if found is not None and found[0] != intended:
        # The direction is not admitted (issue #1496): every value was written back
        # as it was.
        await db.rollback()
        if claims is not None:
            claims.refuse_wrong_direction(L2PortAssignment, row_id, intended)
        return None
    if found is None:
        await db.rollback()
        if claims is not None:
            claims.lost.add(row_id)
        logger.warning(
            "Ignoring stale L2 %s failure for switch %s port %s, reservation %s: "
            "row %s is no longer %s or another pass holds its drive claim "
            "(a concurrent writer won)",
            "build" if intended == "ACTIVE" else "release",
            switch_uuid,
            port,
            res_uuid,
            row_id,
            expected_status,
        )
        return None
    await db.commit()
    return (
        await db.execute(select(L2PortAssignment).where(L2PortAssignment.id == row_id))
    ).scalar_one_or_none()


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
    row_id: uuid.UUID | None = None,
    expected_status: str = "FAILED",
    claims: WiringRowClaims | None = None,
) -> L2PortAssignment | None:
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

    `row_id` (issue #814) switches the write from the key-based upsert to a
    ROW-IDENTITY compare-and-swap, and is what both retry channels pass: a retry
    records its outcome against the very row it loaded, never against whatever row
    the key resolves to now. The write is one `UPDATE ... WHERE id = :id AND status
    = :expected_status`; a `rowcount` of zero means the row was flipped (ACTIVE by a
    racing reattempt, RELEASED by a save-driven teardown) or deleted while the driver
    call was in flight, so nothing is written, nothing is INSERTED, and None comes
    back with the same "a concurrent writer won" warning the #412 guard logs. The key
    match above deliberately excludes RELEASED rows, so without this a stale retry
    failure for a released membership found nothing and took the INSERT branch,
    resurrecting a dead membership as a fresh FAILED intended-ACTIVE zombie row. The
    fresh-build path passes no `row_id` and keeps the upsert: a build after a release
    is a legitimate re-add.

    `claims` (issue #1479) is the calling retry pass's claim ledger, the
    record_l1_failed shape: on the `row_id` path the compare-and-swap also requires a
    row this pass still holds (its stamp unchanged) or one nobody holds; a refused
    write changes nothing, returns None, and adds the row to `claims.lost`. The
    key-based upsert ignores `claims`.

    Issue #1501, the key-based path only, the record_l1_failed rule: a BUILD failure
    with a hardware-retryable reason whose row ends with a real (not nil) allocation,
    landing on a reservation whose wiring is frozen, is recorded intended RELEASED
    with attempts 0, the state the terminal teardown parks such a row in
    (`TEARDOWN_PENDING_VLAN_REMOVAL`), so the release-direction retry channels remove
    whatever the join may have landed. The frozen fact is read inside the UPDATE or
    INSERT itself (`wiring_due.frozen_build_failure_values`). A row with a nil
    allocation, a pinned-reason failure, a release failure, and every write on a
    reservation that is not frozen are unchanged; the row-id path is unchanged.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    vlan_uuid = _as_uuid(vlan_assignment_id) if vlan_assignment_id else None

    if row_id is not None:
        return await _record_l2_failed_for_row(
            db,
            row_id,
            expected_status,
            res_uuid=res_uuid,
            switch_uuid=switch_uuid,
            port=port,
            attempts=attempts,
            last_error=last_error,
            vlan_uuid=vlan_uuid,
            intended=intended,
            claims=claims,
        )

    from app.services.wiring_retry_service import is_retryable_failure

    # Issue #1501: a build failure written on a frozen reservation is recorded as the
    # terminal teardown parks such a row (intended RELEASED, attempts 0), for exactly
    # the rows the teardown parks: a hardware-retryable reason and a real allocation
    # (a nil allocation has no VLAN number a removal could drive, checked per branch).
    build_parks = intended == "ACTIVE" and is_retryable_failure(last_error)

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
        frozen_parks = build_parks and (vlan_uuid or row.vlan_assignment_id) not in (None, _NIL)
        row.status = "FAILED"
        if frozen_parks:
            # Issue #1501: decided in this UPDATE's own statement.
            frozen_values = frozen_build_failure_values(
                L2PortAssignment, func.coalesce(L2PortAssignment.attempts, 0) + attempts
            )
            row.intended = frozen_values["intended"]
            row.attempts = frozen_values["attempts"]
        else:
            row.intended = intended
            row.attempts = (row.attempts or 0) + attempts
        row.last_error = last_error
        # The drive this failure records is over, so release the claim (issue #817).
        row.claimed_until = None
        if vlan_uuid is not None:
            row.vlan_assignment_id = vlan_uuid
        await db.commit()
        if frozen_parks:
            await db.refresh(row)
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
    if build_parks and vlan_uuid not in (None, _NIL):
        # Issue #1501: decided in this INSERT's own statement.
        frozen_values = frozen_build_failure_values(
            L2PortAssignment, attempts, reservation_id=res_uuid
        )
        row.intended = frozen_values["intended"]
        row.attempts = frozen_values["attempts"]
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
    # Parking hands the row to the release channel; drop the claim (issue #817).
    row.claimed_until = None
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
    now: datetime | None = None,
) -> list[L2PortAssignment]:
    """FAILED membership rows still under the attempts cap, oldest first, batch-capped.

    The background auto-retry channel's per-tick L2 candidate set, the due_failed_rows
    analogue. It carries the same drive rule, wiring_due.tick_would_drive (issue
    #1190): a row with a pinned non-retryable reason and a build-direction row of a
    frozen reservation are not candidates, so they cannot fill the batch; the caller
    keeps both checks for a reservation frozen after this select.

    RETRY-ONLY, so it also filters on the issue #817 drive claim and takes FOR UPDATE
    SKIP LOCKED on the ledger table, exactly like due_failed_rows: a row another
    channel is currently driving is not a candidate. The per-row compare-and-swap in
    wiring_claim.WiringRowClaims.claim is the real guard; the reconcile's stale-build
    widening reader deliberately carries none of these predicates (see wiring_claim's
    and wiring_due's module docstrings).
    """
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(L2PortAssignment)
        .where(
            L2PortAssignment.status == "FAILED",
            L2PortAssignment.attempts < max_attempts,
            unclaimed(L2PortAssignment, now),
            tick_would_drive(L2PortAssignment),
        )
        .order_by(L2PortAssignment.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True, of=L2PortAssignment.__table__)
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
