"""L1 cross-connect applied-state projection (ADR 0007 Decision 4, issue #345).

Phase 1 (P3b): the consumer writes an ACTIVE l1_connection_assignments row when
a connect_ports driver call succeeds and flips the row to RELEASED when a
disconnect_ports call succeeds. Nothing consumes the rows yet; this is a new
projection alongside the unchanged execution_runs audit log. Also holds the
frozen-flag setter for reservation_wiring_state (Decision 7) and the migration
backfill reconstruction (a pure, unit-testable helper).
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.services._uuid_utils import as_uuid as _as_uuid

logger = logging.getLogger(__name__)

# The last_error written when a successful build lands AFTER the reservation's wiring
# freeze (issue #461): the hardware is now wired for a dead reservation, so the row is
# parked FAILED intended RELEASED and the release-direction retry channels (which run
# while frozen, ADR 0009 phase 3) drive the disconnect. Deliberately outside the
# non-retryable prefixes in wiring_retry_service so the auto channel sweeps it.
FROZEN_BUILD_PENDING_RELEASE = "connect landed after wiring freeze; pending release"


def canonical_port_pair(port_a: str, port_b: str) -> tuple[str, str]:
    """Order a switch port pair deterministically.

    A cross-connect is undirected: (port_a, port_b) and (port_b, port_a) name the
    same physical pair. Sorting them before insert makes the active-unique index
    on (switch_device_id, port_a, port_b) meaningful, so a redelivery or a
    reversed-order resolve cannot create a second ACTIVE row for the same pair.
    """
    return (port_a, port_b) if port_a <= port_b else (port_b, port_a)


async def record_l1_connect(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port_a: str,
    port_b: str,
    physical_connection_id: uuid.UUID | str | None = None,
) -> L1ConnectionAssignment | None:
    """Insert an ACTIVE assignment after a successful connect_ports.

    Idempotent under redelivery: this reservation's existing ACTIVE row for the same
    (switch, canonical pair) short-circuits, and a concurrent connect that claims
    the pair first trips the active-unique index, whereupon we roll back and
    return the winner's row. Mirrors find_or_assign_vlan's insert-then-catch
    discipline.

    Two guards added by issue #461:

    - Another reservation's ACTIVE row on the same pair is superseded, not adopted:
      an L1 matrix port carries exactly one cross-connect, so THIS successful connect
      proves the old cross-connect no longer exists (the supersede_release_if_reclaimed
      physics, in the build direction). The stale row (typically stranded by a terminal
      teardown that never completed) flips RELEASED and this reservation records its
      own ACTIVE row. Adopting the stale row instead (the pre-#461 behavior) left this
      reservation with NO ledger row, so its own teardown released nothing.
    - If the reservation's wiring is already frozen at record time (the terminal
      handler's freeze landed while our driver call was in flight), the build must NOT
      be recorded ACTIVE: the teardown pass has already snapshotted the ledger and will
      never see it. The row is parked FAILED intended RELEASED
      (FROZEN_BUILD_PENDING_RELEASE) so the release-direction retry channels, which run
      while frozen, tear the just-built cross-connect down. This is the record-time
      analogue of the issue #412 guard: the commit path re-checks the invariant the
      caller checked before the driver call.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    ca, cb = canonical_port_pair(port_a, port_b)

    existing = await _find_active(db, switch_uuid, ca, cb)
    if existing is not None and existing.reservation_id == res_uuid:
        return existing
    if existing is not None:
        # Cross-reservation stale ACTIVE row: superseded by this build (see docstring).
        # Flipped in this session; the commit below (park or insert path) persists both
        # writes together, and an IntegrityError rollback discards both consistently.
        existing.status = "RELEASED"
        existing.intended = "RELEASED"
        existing.released_at = datetime.now(timezone.utc)
        # Flush the release NOW: SQLAlchemy flushes inserts before updates, so without
        # this the ACTIVE insert below would hit the active-unique index while the
        # stale row is still ACTIVE in the database.
        await db.flush()
        logger.warning(
            "Superseding stale ACTIVE L1 row of reservation %s for switch %s pair "
            "(%s, %s): reservation %s proved the pair by a successful connect",
            existing.reservation_id,
            switch_uuid,
            ca,
            cb,
            res_uuid,
        )

    # Record-time freeze re-check (issue #461, the #412 discipline): the selection-time
    # frozen checks (wiring_changed entry, retry-tick row skip) cannot cover a freeze
    # that lands while the driver call is off-loop; only the commit path can.
    state = await get_wiring_state(db, res_uuid)
    if state is not None and state.frozen:
        return await _park_frozen_build(db, res_uuid, switch_uuid, ca, cb)

    # Reuse this reservation's own prior FAILED row for the same pair (a build that
    # failed, then succeeded on a later apply or a retry): flip it ACTIVE in place
    # rather than inserting a parallel row that would leave a stale FAILED sibling
    # (ADR 0007 Decision 6, phase 4). Index-safe because no ACTIVE row exists (checked
    # above), and reservation-scoped so it never adopts another reservation's failure.
    reusable = await _find_reusable_failed(db, res_uuid, switch_uuid, ca, cb)
    if reusable is not None:
        reusable.status = "ACTIVE"
        reusable.intended = "ACTIVE"
        reusable.last_error = None
        reusable.released_at = None
        if physical_connection_id and reusable.physical_connection_id is None:
            reusable.physical_connection_id = _as_uuid(physical_connection_id)
        await db.commit()
        await db.refresh(reusable)
        logger.info(
            "Reattempt flipped FAILED to ACTIVE for switch %s pair (%s, %s), reservation %s",
            switch_uuid,
            ca,
            cb,
            res_uuid,
        )
        return reusable

    assignment = L1ConnectionAssignment(
        reservation_id=res_uuid,
        switch_device_id=switch_uuid,
        port_a=ca,
        port_b=cb,
        physical_connection_id=_as_uuid(physical_connection_id) if physical_connection_id else None,
        status="ACTIVE",
        intended="ACTIVE",
    )
    db.add(assignment)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent connect claimed this pair first (redelivered event). Roll
        # back and return the winner's already-committed row.
        await db.rollback()
        return await _find_active(db, switch_uuid, ca, cb)
    await db.refresh(assignment)
    logger.info(
        "Recorded ACTIVE L1 assignment for switch %s pair (%s, %s), reservation %s",
        switch_uuid,
        ca,
        cb,
        res_uuid,
    )
    return assignment


async def release_l1_connection(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port_a: str,
    port_b: str,
) -> L1ConnectionAssignment | None:
    """Flip this reservation's ACTIVE (or retried-FAILED) row for a pair to RELEASED.

    Returns the released row, or None if no matching row (already released, or
    never recorded). Keyed on reservation plus the canonical pair so a teardown
    only releases what this reservation holds. Matches status ACTIVE or FAILED
    (excludes RELEASED): a FAILED row here is a prior disconnect attempt that did
    not confirm success (intended RELEASED), reattempted through the retry
    channel; a successful reattempt flips it RELEASED in place, the release-side
    mirror of record_l1_connect's reusable-FAILED flip for the build direction.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    ca, cb = canonical_port_pair(port_a, port_b)

    result = await db.execute(
        select(L1ConnectionAssignment).where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == ca,
            L1ConnectionAssignment.port_b == cb,
            L1ConnectionAssignment.status != "RELEASED",
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.status = "RELEASED"
    row.intended = "RELEASED"
    row.last_error = None
    row.released_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "Released L1 assignment for switch %s pair (%s, %s), reservation %s",
        switch_uuid,
        ca,
        cb,
        res_uuid,
    )
    return row


async def _find_active(
    db: AsyncSession, switch_uuid: uuid.UUID, port_a: str, port_b: str
) -> L1ConnectionAssignment | None:
    result = await db.execute(
        select(L1ConnectionAssignment).where(
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == port_a,
            L1ConnectionAssignment.port_b == port_b,
            L1ConnectionAssignment.status == "ACTIVE",
        )
    )
    return result.scalar_one_or_none()


async def _find_reusable_failed(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    switch_uuid: uuid.UUID,
    port_a: str,
    port_b: str,
) -> L1ConnectionAssignment | None:
    """This reservation's non-RELEASED (FAILED) row for a pair, if any.

    The candidate a successful (re)connect flips to ACTIVE in place. RELEASED rows
    are excluded so a torn-down cross-connect is never resurrected.
    """
    result = await db.execute(
        select(L1ConnectionAssignment).where(
            L1ConnectionAssignment.reservation_id == reservation_id,
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == port_a,
            L1ConnectionAssignment.port_b == port_b,
            L1ConnectionAssignment.status == "FAILED",
        )
    )
    return result.scalars().first()


async def _park_frozen_build(
    db: AsyncSession,
    res_uuid: uuid.UUID,
    switch_uuid: uuid.UUID,
    port_a: str,
    port_b: str,
) -> L1ConnectionAssignment:
    """Park a post-freeze successful build as FAILED intended RELEASED (issue #461).

    The connect succeeded on hardware but the reservation's wiring froze while the
    driver call was in flight, so the terminal teardown never saw (and never will see)
    this pair. Recording it ACTIVE would strand live switch config forever; parking it
    FAILED intended RELEASED hands it to the release-direction retry channels, which
    are allowed to run on a frozen reservation (ADR 0009 phase 3) and drive the
    disconnect. attempts are NOT incremented: the release direction has not failed yet,
    and inflating the counter would push the pending disconnect toward the auto-retry
    cap for no reason. Ports arrive already canonical from record_l1_connect.
    """
    result = await db.execute(
        select(L1ConnectionAssignment).where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == port_a,
            L1ConnectionAssignment.port_b == port_b,
            L1ConnectionAssignment.status != "RELEASED",
        )
    )
    row = result.scalars().first()
    if row is None:
        row = L1ConnectionAssignment(
            reservation_id=res_uuid,
            switch_device_id=switch_uuid,
            port_a=port_a,
            port_b=port_b,
            status="FAILED",
            intended="RELEASED",
            attempts=0,
            last_error=FROZEN_BUILD_PENDING_RELEASE,
        )
        db.add(row)
    else:
        row.status = "FAILED"
        row.intended = "RELEASED"
        row.last_error = FROZEN_BUILD_PENDING_RELEASE
    await db.commit()
    await db.refresh(row)
    logger.warning(
        "Build for frozen reservation %s landed on switch %s pair (%s, %s); "
        "parked FAILED intended RELEASED for the release retry channel",
        res_uuid,
        switch_uuid,
        port_a,
        port_b,
    )
    return row


async def freeze_reservation_wiring(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> ReservationWiringState:
    """Set reservation_wiring_state.frozen = true, upserting the row if absent.

    Called by the terminal-event handlers (Decision 7). Phase 1 write is inert:
    nothing reads frozen yet, but the marker is durable for the phase 3
    wiring_changed no-op guard. Upsert is read-then-insert with an IntegrityError
    fallback so two terminal events for one reservation cannot both insert.
    """
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(ReservationWiringState).where(ReservationWiringState.reservation_id == res_uuid)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.frozen = True
        await db.commit()
        return row

    row = ReservationWiringState(reservation_id=res_uuid, frozen=True)
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(ReservationWiringState).where(ReservationWiringState.reservation_id == res_uuid)
        )
        row = result.scalar_one()
        row.frozen = True
        await db.commit()
    return row


async def get_wiring_state(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> ReservationWiringState | None:
    """Read a reservation's wiring-state row, or None when it does not exist.

    A missing row is the pre-P3b / phase-1-deferred case: phase 1 only ever
    creates the row via freeze_reservation_wiring on a terminal event, so an
    absent row means no terminal event fired and no version was applied. The
    phase 3 consumer treats a missing row as a gap (ADR 0007 Decision 4).
    """
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(ReservationWiringState).where(ReservationWiringState.reservation_id == res_uuid)
    )
    return result.scalar_one_or_none()


async def stamp_last_applied(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    fork_version: int,
) -> ReservationWiringState:
    """Advance reservation_wiring_state.last_applied_fork_version, upserting the row.

    The monotonic ordering marker (ADR 0007 Decision 4). Called after a version is
    processed, even when the pass left FAILED rows: the version was processed, so a
    replay of the same version is a no-op skip. Never lowers the marker (a stale
    stamp cannot regress it) and never clears frozen. Upsert is read-then-insert with
    an IntegrityError fallback so a concurrent first-write cannot double-insert.
    """
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(ReservationWiringState).where(ReservationWiringState.reservation_id == res_uuid)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        if row.last_applied_fork_version is None or fork_version > row.last_applied_fork_version:
            row.last_applied_fork_version = fork_version
            await db.commit()
        return row

    row = ReservationWiringState(
        reservation_id=res_uuid,
        last_applied_fork_version=fork_version,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(ReservationWiringState).where(ReservationWiringState.reservation_id == res_uuid)
        )
        row = result.scalar_one()
        if row.last_applied_fork_version is None or fork_version > row.last_applied_fork_version:
            row.last_applied_fork_version = fork_version
            await db.commit()
    return row


async def active_assignments_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[L1ConnectionAssignment]:
    """Return this reservation's ACTIVE cross-connect rows (the applied L1 set)."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(L1ConnectionAssignment).where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.status == "ACTIVE",
        )
    )
    return list(result.scalars().all())


async def all_assignments_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[L1ConnectionAssignment]:
    """Return every assignment row for a reservation (all statuses), oldest first.

    Backs the phase-4 wiring-status surface: the caller wants the full per-connection
    picture (ACTIVE cross-connects, RELEASED history, and FAILED rows needing retry),
    so no status filter is applied. Ordered by created_at for a stable read.
    """
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(L1ConnectionAssignment)
        .where(L1ConnectionAssignment.reservation_id == res_uuid)
        .order_by(L1ConnectionAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def failed_assignments_for_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
) -> list[L1ConnectionAssignment]:
    """Return this reservation's FAILED rows, oldest first (the retry candidates)."""
    res_uuid = _as_uuid(reservation_id)
    result = await db.execute(
        select(L1ConnectionAssignment)
        .where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.status == "FAILED",
        )
        .order_by(L1ConnectionAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def due_failed_rows(
    db: AsyncSession,
    limit: int,
    max_attempts: int,
) -> list[L1ConnectionAssignment]:
    """FAILED rows still under the total-attempts cap, oldest first, batch-capped.

    The background auto-retry channel's per-tick candidate set (ADR 0007 Decision 6
    item 2): FAILED rows whose accumulated `attempts` is below `max_attempts`, so a
    row parked past the cap is never re-swept (manual retry only). The LIMIT bounds
    one tick exactly as the health scheduler's batch cap does. Retryability by reason
    and the frozen-reservation guard are applied by the caller, since neither is a
    clean SQL predicate here.
    """
    result = await db.execute(
        select(L1ConnectionAssignment)
        .where(
            L1ConnectionAssignment.status == "FAILED",
            L1ConnectionAssignment.attempts < max_attempts,
        )
        .order_by(L1ConnectionAssignment.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def is_pair_active(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port_a: str,
    port_b: str,
) -> bool:
    """True when this reservation already holds an ACTIVE row for the pair.

    The idempotency gate for a build (ADR 0007 Decision 4/5): a build whose pair is
    already ACTIVE for this reservation is a convergent no-op, not a driver call.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    ca, cb = canonical_port_pair(port_a, port_b)
    result = await db.execute(
        select(L1ConnectionAssignment.id).where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == ca,
            L1ConnectionAssignment.port_b == cb,
            L1ConnectionAssignment.status == "ACTIVE",
        )
    )
    return result.first() is not None


async def pair_needs_release(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port_a: str,
    port_b: str,
) -> bool:
    """True when this pair is believed to still be live and a disconnect belongs.

    The idempotency gate for a release (issue #369): unlike is_pair_active (build's
    ACTIVE-only convergence check), this is also true for a FAILED row whose
    intended is RELEASED. Such a row IS a disconnect attempt that never confirmed
    success, so the pair may still be physically connected; treating it as "nothing
    to release" (is_pair_active would say False, since status != ACTIVE) would make
    a release-direction retry a silent no-op, never reaching the driver. A FAILED
    row whose intended is ACTIVE (a build that never connected) is correctly
    excluded: there is nothing live to release.
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    ca, cb = canonical_port_pair(port_a, port_b)
    result = await db.execute(
        select(L1ConnectionAssignment.status, L1ConnectionAssignment.intended).where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == ca,
            L1ConnectionAssignment.port_b == cb,
            L1ConnectionAssignment.status != "RELEASED",
        )
    )
    row = result.first()
    if row is None:
        return False
    status, intended = row
    return status == "ACTIVE" or (status == "FAILED" and intended == "RELEASED")


async def supersede_release_if_reclaimed(
    db: AsyncSession,
    row: L1ConnectionAssignment,
) -> bool:
    """Settle a release-direction FAILED row as RELEASED, no driver call, when a newer
    build already reclaimed its port on the same switch (ADR 0009 phase 3, issue #369).

    A release-direction FAILED row (intended RELEASED) is a disconnect that never
    confirmed success, so this reservation's own ledger still believes the pair may be
    live. But an L1 matrix port carries exactly one cross-connect: if ANOTHER
    reservation now holds an ACTIVE row on the same switch claiming either canonical
    port of this pair, that newer build could only have succeeded by first destroying
    the old cross-connect. The disconnect this row wants is therefore already done in
    hardware; re-firing disconnect_ports would at best no-op and at worst tear down the
    newer reservation's live cross-connect. So we flip the row RELEASED in place and
    skip the driver.

    pair_needs_release cannot see this: it is reservation-scoped (it inspects only THIS
    reservation's row for the pair) and correctly reports the pair as still needing a
    release, since from this reservation's ledger the disconnect never confirmed. The
    superseding evidence lives in a DIFFERENT reservation's ACTIVE row, which only a
    cross-reservation, port-level query reaches. Returns True when the row was
    superseded (and now RELEASED), False when a driver disconnect is still warranted.
    """
    ca, cb = canonical_port_pair(row.port_a, row.port_b)
    result = await db.execute(
        select(L1ConnectionAssignment.id).where(
            L1ConnectionAssignment.switch_device_id == row.switch_device_id,
            L1ConnectionAssignment.reservation_id != row.reservation_id,
            L1ConnectionAssignment.status == "ACTIVE",
            or_(
                L1ConnectionAssignment.port_a.in_((ca, cb)),
                L1ConnectionAssignment.port_b.in_((ca, cb)),
            ),
        )
    )
    if result.first() is None:
        return False
    await release_l1_connection(
        db, row.reservation_id, row.switch_device_id, row.port_a, row.port_b
    )
    return True


async def record_l1_failed(
    db: AsyncSession,
    reservation_id: uuid.UUID | str,
    switch_device_id: uuid.UUID | str,
    port_a: str,
    port_b: str,
    attempts: int,
    last_error: str,
    physical_connection_id: uuid.UUID | str | None = None,
    *,
    intended: str,
) -> L1ConnectionAssignment:
    """Write (or update) a FAILED row for a cross-connect that could not be applied.

    `intended` (ADR 0009 Decision 2, issue #369) is the direction THIS write was
    attempting: "ACTIVE" for a failed connect/build, "RELEASED" for a failed
    disconnect/release. Every caller must pass it explicitly (no default) so a
    build failure and a release failure are never conflated: the retry channels
    (wiring_retry_service) split FAILED rows on this field to reattempt each one
    in its own direction, instead of defaulting every FAILED row to a reconnect
    (the bug issue #369 describes).

    A FAILED status is exempt from the ACTIVE-only partial-unique index, so a FAILED
    row never blocks a later ACTIVE claim on the same pair and cannot collide with the
    live cross-connect. Upserts on (reservation, switch, canonical pair) for any
    non-RELEASED row so a redelivery before the version is stamped, or a phase-4
    reattempt, updates the existing row rather than duplicating. `attempts` ACCUMULATES
    onto the existing row (the driver attempts made in THIS pass are added to the
    running total), so the phase-4 total-attempts cap counts every driver call ever
    spent on the connection, across the in-line apply and every reattempt. Feeds the
    Decision 6 per-connection retry/manual channel (phase 4).
    """
    res_uuid = _as_uuid(reservation_id)
    switch_uuid = _as_uuid(switch_device_id)
    ca, cb = canonical_port_pair(port_a, port_b)

    result = await db.execute(
        select(L1ConnectionAssignment).where(
            L1ConnectionAssignment.reservation_id == res_uuid,
            L1ConnectionAssignment.switch_device_id == switch_uuid,
            L1ConnectionAssignment.port_a == ca,
            L1ConnectionAssignment.port_b == cb,
            L1ConnectionAssignment.status != "RELEASED",
        )
    )
    row = result.scalars().first()
    if row is not None:
        if row.status == "ACTIVE" and intended == "ACTIVE":
            # The issue #412 guard, scoped to the direction it actually protects:
            # a racing writer (manual retry, phase-4 reattempt, or a concurrent
            # apply) proved this pair connects AFTER our failed BUILD attempt
            # began, so this failure is stale information: never downgrade the
            # winner (the #276/#318 CAS discipline). attempts and intended stay
            # untouched by the refused write: an ACTIVE row is immutable to a
            # stale connect-direction failure writer, and inflating attempts
            # would push a healthy pair toward the retry cap for no reason.
            #
            # This does NOT extend to a RELEASE-direction failure (intended
            # RELEASED): an ACTIVE row there is not a race, it is the normal,
            # expected starting state of a disconnect that failed (the pair is
            # still genuinely connected), and recording that as a retryable
            # FAILED row is the entire point of issue #369. See
            # pair_needs_release for the matching release-side idempotency gate.
            logger.warning(
                "Ignoring stale L1 build failure for switch %s pair (%s, %s), "
                "reservation %s: row is ACTIVE (a concurrent writer won)",
                switch_uuid,
                ca,
                cb,
                res_uuid,
            )
            return row
        row.status = "FAILED"
        row.intended = intended
        row.attempts = (row.attempts or 0) + attempts
        row.last_error = last_error
        if physical_connection_id and row.physical_connection_id is None:
            row.physical_connection_id = _as_uuid(physical_connection_id)
        await db.commit()
        return row

    row = L1ConnectionAssignment(
        reservation_id=res_uuid,
        switch_device_id=switch_uuid,
        port_a=ca,
        port_b=cb,
        physical_connection_id=_as_uuid(physical_connection_id) if physical_connection_id else None,
        status="FAILED",
        intended=intended,
        attempts=attempts,
        last_error=last_error,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def park_stale_l1_build(
    db: AsyncSession,
    assignment_id: uuid.UUID,
    reason: str,
) -> L1ConnectionAssignment | None:
    """Flip a FAILED intended-ACTIVE row to intended RELEASED: its build intent is gone.

    Issue #491: the pair this build was trying to establish is no longer in cabling's
    intended set (a fork re-save removed the connection), so the build must never be
    reattempted. The row stays FAILED but flips to intended RELEASED so the
    release-direction channels (which pair_needs_release now admits) drive the idempotent
    disconnect that settles whatever the failed build may have half-applied. attempts
    RESET to 0 (the #479 rationale, mirrored from _park_frozen_build: the release
    direction has not failed yet, and a carried-over build count would push the pending
    disconnect toward the auto-retry cap for no reason). A row no longer FAILED is left
    untouched: ACTIVE means a concurrent writer proved the pair connects (the #412
    discipline; the widened reconcile release settles it on a later pass), RELEASED means
    it is already settled.
    """
    result = await db.execute(
        select(L1ConnectionAssignment).where(L1ConnectionAssignment.id == assignment_id)
    )
    row = result.scalar_one_or_none()
    if row is None or row.status != "FAILED":
        return row
    row.intended = "RELEASED"
    row.attempts = 0
    row.last_error = reason
    await db.commit()
    logger.warning(
        "Build intent gone for L1 assignment %s (switch %s pair (%s, %s), reservation %s); "
        "parked FAILED intended RELEASED for the release-direction channels",
        row.id,
        row.switch_device_id,
        row.port_a,
        row.port_b,
        row.reservation_id,
    )
    return row


def _row_get(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _strictly_after(a, b) -> bool:
    """True if timestamp a is strictly after b, treating None as oldest.

    A None created_at (should not occur, created_at is server-defaulted) is
    ordered before any real timestamp so a real event always wins; two Nones do
    not order, so the incumbent is kept and a co-None disconnect is treated as a
    teardown (conservative: a pair we cannot prove is live is left out).
    """
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def compute_backfill_assignments(runs) -> list[dict]:
    """Reconstruct currently-live L1 cross-connects from execution_run rows.

    `runs` is any iterable of rows exposing reservation_id, device_id (the
    switch), action, status, port_a, port_b, created_at (dicts or attribute
    objects both work). This mirrors the execution_runs inference the retired
    action_succeeded_for_reservation helper used (a SUCCESS connect_ports means the
    pair was cross-connected), refined to a projection of CURRENT state: a pair is live
    only when its latest SUCCESS connect_ports is not followed by a SUCCESS
    disconnect_ports. Keying on (switch_device_id, canonical pair) yields at most
    one ACTIVE row per pair, so the backfill can never violate the active-unique
    index; the owning reservation is the one holding the latest live connect.

    Returns a list of dicts with reservation_id, switch_device_id, port_a, port_b
    (canonical order). physical_connection_id is never reconstructable from
    execution_runs, so the caller inserts it as null.
    """
    latest_connect: dict[tuple, tuple] = {}
    latest_disconnect: dict[tuple, object] = {}

    for r in runs:
        if _row_get(r, "status") != "SUCCESS":
            continue
        action = _row_get(r, "action")
        if action not in ("connect_ports", "disconnect_ports"):
            continue
        switch_id = _row_get(r, "device_id")
        port_a = _row_get(r, "port_a")
        port_b = _row_get(r, "port_b")
        if switch_id is None or port_a is None or port_b is None:
            continue
        ca, cb = canonical_port_pair(str(port_a), str(port_b))
        key = (switch_id, ca, cb)
        created = _row_get(r, "created_at")
        if action == "connect_ports":
            prev = latest_connect.get(key)
            if prev is None or _strictly_after(created, prev[0]):
                latest_connect[key] = (created, _row_get(r, "reservation_id"))
        else:
            prev = latest_disconnect.get(key)
            if prev is None or _strictly_after(created, prev):
                latest_disconnect[key] = created

    result: list[dict] = []
    for key, (c_created, reservation_id) in latest_connect.items():
        if reservation_id is None:
            # No reservation to scope the row to; nothing counts as applied.
            continue
        d_created = latest_disconnect.get(key)
        if d_created is not None and not _strictly_after(c_created, d_created):
            # A disconnect at or after the latest connect: the pair is torn down.
            continue
        switch_id, ca, cb = key
        result.append(
            {
                "reservation_id": reservation_id,
                "switch_device_id": switch_id,
                "port_a": ca,
                "port_b": cb,
            }
        )
    return result


def compute_backfill_intended(failed_rows, runs) -> dict:
    """Reconstruct intended for existing FAILED l1_connection_assignments rows.

    Migration 0016's data step (issue #369, ADR 0009 Decision 2): a FAILED row
    that predates the `intended` column is reconstructed from the most recent
    connect_ports/disconnect_ports execution_run for the same (reservation,
    switch device, canonical port pair), REGARDLESS of that run's own status
    (unlike compute_backfill_assignments, which only considers SUCCESS runs to
    infer current live state; here we want the last ATTEMPTED direction, and the
    row is FAILED precisely because that attempt did not succeed).
    connect_ports means intended ACTIVE, disconnect_ports means intended
    RELEASED. A FAILED row with no matching run defaults to ACTIVE, matching the
    column's server_default.

    `failed_rows` exposes id, reservation_id, switch_device_id, port_a, port_b
    (already canonical). `runs` exposes reservation_id, device_id (the switch),
    action, port_a, port_b, created_at. Dicts or attribute objects both work for
    either iterable. Returns {row_id: "ACTIVE" | "RELEASED"}.

    Kept in sync with the frozen copy embedded in migration 0016: migrations must
    not import live application code (a later refactor of this module would break
    `alembic upgrade` for deployments still below that revision), so the two
    copies are intentionally duplicated, not shared.
    """
    latest: dict[tuple, tuple] = {}
    for r in runs:
        action = _row_get(r, "action")
        if action not in ("connect_ports", "disconnect_ports"):
            continue
        reservation_id = _row_get(r, "reservation_id")
        switch_id = _row_get(r, "device_id")
        port_a = _row_get(r, "port_a")
        port_b = _row_get(r, "port_b")
        if reservation_id is None or switch_id is None or port_a is None or port_b is None:
            continue
        ca, cb = canonical_port_pair(str(port_a), str(port_b))
        key = (reservation_id, switch_id, ca, cb)
        created = _row_get(r, "created_at")
        prev = latest.get(key)
        if prev is None or _strictly_after(created, prev[0]):
            latest[key] = (created, action)

    result: dict = {}
    for row in failed_rows:
        row_id = _row_get(row, "id")
        key = (
            _row_get(row, "reservation_id"),
            _row_get(row, "switch_device_id"),
            _row_get(row, "port_a"),
            _row_get(row, "port_b"),
        )
        match = latest.get(key)
        if match is None:
            result[row_id] = "ACTIVE"
        else:
            result[row_id] = "ACTIVE" if match[1] == "connect_ports" else "RELEASED"
    return result
