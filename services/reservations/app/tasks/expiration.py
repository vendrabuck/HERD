"""Background task that auto-activates and auto-completes reservations."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from herd_common.internal_client import InternalTokenAuth, call_service
from herd_common.outbox import enqueue_event
from herd_common.retry import retry_with_backoff
from sqlalchemy import and_, exists, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.fork_wiring_ledger import ForkWiringLedger
from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
)
from app.services.purpose_service import stamp_purpose_classify_requested
from app.services.reservation_service import (
    _archive_reservation_fork_best_effort,
    _claim_provision_transition,
    _clear_pending_fork_prune,
    _create_reservation_fork_best_effort,
    _fetch_active_forks,
    _fetch_devices_best_effort,
    _provision_requested_event,
    _prune_removed_devices_from_fork_best_effort,
    _release_exclusive_devices_best_effort,
    _reservation_created_event,
    _reservation_failed_event,
    _update_device_statuses,
    stage_wiring_changed,
)

# Terminal reservation statuses whose fork the standing reconciler may archive.
TERMINAL_STATUSES = (
    ReservationStatus.COMPLETED,
    ReservationStatus.CANCELLED,
    ReservationStatus.FAILED,
)

logger = logging.getLogger(__name__)

EXPIRING_SOON_SUBJECT = "herd.reservations.expiring_soon"
COMPLETED_SUBJECT = "herd.reservations.completed"
CREATED_SUBJECT = "herd.reservations.created"
FAILED_SUBJECT = "herd.reservations.failed"
PROVISION_REQUESTED_SUBJECT = "herd.reservations.provision_requested"


async def _run_reminder_cycle() -> None:
    """Stage one reservation.expiring_soon event per reservation in the lead window.

    Selects ACTIVE reservations whose end_time is in the future but within
    `expiry_reminder_lead_seconds` of now and that have not been reminded yet
    (expiry_reminder_sent_at is null). Stamps the timestamp and stages the
    outbox event inside the same transaction (issue #21), so a row is claimed
    and its event durably enqueued atomically; this dedupes the reminder per
    reservation across ticks. A lead window of 0 disables the reminder entirely.

    The relay publishes the staged event; because the stamp and the event commit
    together, a reminded reservation is never reminded again, matching the
    at-most-once intent (a missed publish is impossible now, and a duplicate is
    prevented by the stamp).
    """
    lead = settings.expiry_reminder_lead_seconds
    if lead <= 0:
        return

    now = datetime.now(timezone.utc)
    threshold = now + timedelta(seconds=lead)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Reservation).where(
                and_(
                    Reservation.status == ReservationStatus.ACTIVE,
                    Reservation.expiry_reminder_sent_at.is_(None),
                    Reservation.end_time > now,
                    Reservation.end_time <= threshold,
                )
            )
        )
        due = result.scalars().all()
        # Stamp the row and stage its event in the same transaction. The
        # eager-loaded devices are still attached here, so the payload is built
        # before the commit.
        for res in due:
            enqueue_event(
                db,
                OutboxEvent,
                EXPIRING_SOON_SUBJECT,
                {
                    "event": "reservation.expiring_soon",
                    "reservation_id": str(res.id),
                    "user_id": str(res.user_id),
                    "device_ids": [str(d) for d in res.device_ids],
                    "end_time": res.end_time.isoformat(),
                    "purpose_category": res.purpose_category,
                },
            )
            res.expiry_reminder_sent_at = now
            logger.info(
                "Staging expiring_soon reminder for reservation %s",
                res.id,
                extra={
                    "action": "reservation_expiring_soon",
                    "reservation_id": str(res.id),
                },
            )
        await db.commit()


async def _activate_pending_reservation(reservation_id: uuid.UUID) -> bool:
    """Provision and activate one claimed (PENDING_PROVISION) reservation.

    This is the deferred half of create_reservation's immediate path (issue #132):
    a scheduled booking is created PENDING, claimed to PENDING_PROVISION by the
    cycle below at start_time, then handed here to run exactly what a start-now
    reservation runs: flip its exclusive devices to RESERVED in inventory, mark it
    ACTIVE, create the editable fork, and emit reservation.created so execution
    provisions (L1 connect, L2 VLAN) and notifications fire.

    Failure policy is retry-next-tick (decision for #132): if inventory is
    unreachable after retries, the claim is reverted to PENDING and False is
    returned, so a later cycle retries rather than permanently FAILing a valid
    booking. The inventory flip (idempotent re-RESERVED), fork (idempotent on
    reservation_id), and the single reservation.created emit (only after a
    successful flip) make the retry safe.
    """
    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None or res.status != ReservationStatus.PENDING_PROVISION:
            # Already activated/cancelled or claimed by another instance.
            return False
        device_ids = list(res.device_ids)
        topology_id = res.topology_id
        user_id = res.user_id

    # Flip exclusive devices to RESERVED, outside any open transaction. Exclusivity
    # is resolved via the internal-token fetch (same conservative assume-exclusive
    # on fetch failure as the completion path), since the task acts as the system.
    fetch_results = await _fetch_devices_best_effort(device_ids)
    exclusive_ids: list[uuid.UUID] = []
    for did, result in zip(device_ids, fetch_results):
        if isinstance(result, BaseException) or result.get("exclusive", True):
            exclusive_ids.append(did)

    if exclusive_ids:
        try:
            await retry_with_backoff(
                lambda: _update_device_statuses(exclusive_ids, "RESERVED", raise_on_failure=True),
                attempts=3,
                initial_delay=0.5,
                factor=2.0,
                max_delay=5.0,
            )
        except Exception:
            # Revert the claim so a later tick retries (retry-next-tick policy).
            async with AsyncSessionLocal() as db:
                res = await db.get(Reservation, reservation_id)
                if res is not None and res.status == ReservationStatus.PENDING_PROVISION:
                    res.status = ReservationStatus.PENDING
                    await db.commit()
            logger.warning(
                "Scheduled activation deferred for %s: inventory flip failed; retry next tick",
                reservation_id,
                extra={
                    "action": "scheduled_activation_deferred",
                    "reservation_id": str(reservation_id),
                },
            )
            return False

    # Mark ACTIVE and stage reservation.created in the same transaction (issue
    # #21), so the event commits atomically with the ACTIVE row. The payload is
    # built while the row (and its eager-loaded devices) is attached, before the
    # commit.
    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None or res.status != ReservationStatus.PENDING_PROVISION:
            return False
        if res.dynamic_requests:
            # Gated activation (ADR 0004): a dynamic-carrying reservation stays
            # PENDING_PROVISION with its devices RESERVED; stage
            # provision_requested and let the execution service's callback (or
            # the timeout backstop) own the next transition. The fork and
            # reservation.created follow in the callback path. Publishing after
            # the flip (not at the claim) keeps the retry-next-tick revert
            # above from orphaning instances against a PENDING reservation.
            enqueue_event(
                db, OutboxEvent, PROVISION_REQUESTED_SUBJECT, _provision_requested_event(res)
            )
            await db.commit()
            logger.info(
                "Scheduled dynamic reservation handed to provisioning: %s",
                reservation_id,
                extra={
                    "action": "scheduled_provision_requested",
                    "reservation_id": str(reservation_id),
                },
            )
            return True
        res.status = ReservationStatus.ACTIVE
        enqueue_event(db, OutboxEvent, CREATED_SUBJECT, _reservation_created_event(res))
        await db.commit()

    # Editable per-reservation fork (best-effort, never raises), mirroring
    # create_reservation's step 8. The reservation.created event is already
    # staged above and the relay will publish it.
    await _create_reservation_fork_best_effort(reservation_id, topology_id, created_by=str(user_id))
    logger.info(
        "Scheduled reservation activated: %s",
        reservation_id,
        extra={"action": "scheduled_activation", "reservation_id": str(reservation_id)},
    )
    return True


async def _run_expiration_cycle() -> None:
    """Single expiration cycle: activate pending, complete expired.

    Auto-completion is the normal end-of-life path for a reservation (most end by
    reaching end_time, not by manual release). For each reservation it completes,
    this stages a reservation.completed event on the same subject and with the
    same payload shape as the manual release path (release_reservation), so the
    execution service deprovisions (L1 disconnect, L2 VLAN teardown) and
    notifications renders the completion. Without it the devices are flipped to
    AVAILABLE in inventory while their config stays wired, letting a new
    reservation be booked on top of stale, never-torn-down config.

    The event is staged in the same transaction that lands the reservation in
    COMPLETED (issue #21), so the event exists iff the completion committed; the
    relay publishes it. The inventory release below is best-effort and runs after
    the commit.
    """
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        # Claim PENDING reservations whose start_time has passed by moving them to
        # PENDING_PROVISION (the documented PENDING -> PENDING_PROVISION -> ACTIVE
        # path). skip_locked so concurrent service instances do not double-claim;
        # it is a no-op on SQLite (unit tests). Provisioning runs after this
        # transaction commits and the row lock is released, never during HTTP.
        result = await db.execute(
            select(Reservation)
            .where(
                and_(
                    Reservation.status == ReservationStatus.PENDING,
                    Reservation.start_time <= now,
                )
            )
            .with_for_update(skip_locked=True)
        )
        claimed = result.scalars().all()
        for res in claimed:
            res.status = ReservationStatus.PENDING_PROVISION
            logger.info(
                "Claimed scheduled reservation %s for activation",
                res.id,
                extra={"action": "scheduled_activation_claim", "reservation_id": str(res.id)},
            )
        activate_ids = [res.id for res in claimed]

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
        # Stage the completion event in the same transaction that lands COMPLETED.
        # Mirror the manual release_reservation payload exactly: same subject,
        # event name, and fields, so execution and notifications consumers handle
        # an auto-expiry identically to a manual release. device_ids is the
        # reservation's full set (every device in the topology), not just the
        # exclusive subset released in inventory below. The eager-loaded
        # device_ids is read here while the row is attached, before the commit.
        for res in expired:
            res.status = ReservationStatus.COMPLETED
            # One of the five terminal-transition sites (issue #646 phase 2,
            # ADR 0013 point 8): marks the row eligible for background purpose
            # classification.
            stamp_purpose_classify_requested(res)
            enqueue_event(
                db,
                OutboxEvent,
                COMPLETED_SUBJECT,
                {
                    "event": "reservation.completed",
                    "reservation_id": str(res.id),
                    "user_id": str(res.user_id),
                    "device_ids": [str(d) for d in res.device_ids],
                    "topology_id": str(res.topology_id) if res.topology_id else None,
                    "topology_type": res.topology_type.value,
                    "purpose_category": res.purpose_category,
                },
            )
            logger.info(
                "Auto-completed reservation %s",
                res.id,
                extra={"action": "auto_complete", "reservation_id": str(res.id)},
            )

        # Timeout backstop (ADR 0004): fail dynamic-carrying reservations stuck
        # in PENDING_PROVISION past provision_timeout_seconds, so a lost
        # provision-result callback never strands a reservation. updated_at is
        # the transition timestamp: nothing touches a stuck row after it enters
        # PENDING_PROVISION. reservation.failed drives execution-side instance
        # teardown. A timeout of 0 disables both backstops rather than instantly
        # reclaiming every in-flight provisioning. Physical-only rows take the
        # revert branch below, not this failing one.
        stuck: list[Reservation] = []
        if settings.provision_timeout_seconds > 0:
            deadline = now - timedelta(seconds=settings.provision_timeout_seconds)
            result = await db.execute(
                select(Reservation).where(
                    and_(
                        Reservation.status == ReservationStatus.PENDING_PROVISION,
                        Reservation.updated_at <= deadline,
                        exists().where(ReservationDynamicRequest.reservation_id == Reservation.id),
                    )
                )
            )
            candidates = list(result.scalars().all())
            for res in candidates:
                # Compare-and-swap the transition (issue #276). A provision-result
                # callback that committed ACTIVE (or FAILED) between the SELECT
                # above and here wins the row instead, and our conditional UPDATE
                # matches zero rows: skip it, staging no duplicate reservation.failed
                # and, crucially, never tearing down instances the callback just
                # activated. Only rows this call actually failed join `stuck`, so
                # the release loop below never touches a reservation another
                # writer just activated.
                if not await _claim_provision_transition(db, res.id, ReservationStatus.FAILED):
                    continue
                stuck.append(res)
                # One of the five terminal-transition sites (issue #646 phase 2,
                # ADR 0013 point 8): marks the row eligible for background
                # purpose classification. The CAS above bypasses the ORM's
                # in-memory status sync, but this column is untouched by it, so
                # setting it here on `res` and committing below is safe.
                stamp_purpose_classify_requested(res)
                enqueue_event(db, OutboxEvent, FAILED_SUBJECT, _reservation_failed_event(res))
                logger.error(
                    "Provisioning timed out for reservation %s; failing it",
                    res.id,
                    extra={
                        "action": "provision_timeout_failed",
                        "reservation_id": str(res.id),
                    },
                )

            # Restart backstop (issue #318): revert physical-only reservations
            # stranded in PENDING_PROVISION past the same deadline back to PENDING,
            # so a later cycle's claim path re-runs the inventory flip and
            # activation. A physical-only reservation resolves inline (the
            # immediate create path, or the scheduled claim's
            # _activate_pending_reservation), never via a callback, so a process
            # restart between the PENDING_PROVISION commit and that resolving
            # transition otherwise strands the row forever with nothing to reclaim
            # it. Unlike the dynamic backstop this reverts rather than fails: no
            # reservation.created was emitted yet (it commits atomically with the
            # ACTIVE transition), so no execution provisioning ran and there is
            # nothing to tear down; re-activation re-flips the exclusive devices
            # idempotently. PENDING is in the conflict set, so the window stays
            # held across the revert. The NOT EXISTS mirrors the dynamic branch's
            # EXISTS, so the two backstops partition PENDING_PROVISION and never
            # both touch one row.
            result = await db.execute(
                select(Reservation).where(
                    and_(
                        Reservation.status == ReservationStatus.PENDING_PROVISION,
                        Reservation.updated_at <= deadline,
                        ~exists().where(ReservationDynamicRequest.reservation_id == Reservation.id),
                    )
                )
            )
            for res in result.scalars().all():
                # Same compare-and-swap discipline (issue #276): an in-process
                # activation that committed ACTIVE (immediate create path or
                # _activate_pending_reservation) between the SELECT above and here
                # wins the row, our conditional UPDATE matches zero rows, and we
                # skip it. The revert only lands when nothing else resolved the
                # row, i.e. the genuine restart-strand case.
                if not await _claim_provision_transition(db, res.id, ReservationStatus.PENDING):
                    continue
                logger.warning(
                    "Reclaiming stranded physical reservation %s; reverting to PENDING",
                    res.id,
                    extra={
                        "action": "provision_restart_reclaim",
                        "reservation_id": str(res.id),
                    },
                )

        await db.commit()

    # Provision each claimed reservation now that the claim is committed and the
    # row lock is released: flip inventory, mark ACTIVE, fork, emit
    # reservation.created (issue #132). Each is independent and best-effort; a
    # deferred one stays PENDING for a later tick.
    for reservation_id in activate_ids:
        await _activate_pending_reservation(reservation_id)

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
        # Freeze the fork as the as-built record now the reservation is COMPLETED
        # (ADR 0006 Decision 5). Best-effort, mirroring the manual release path.
        await _archive_reservation_fork_best_effort(res.id)

    # Release the timed-out reservations' exclusive devices back to AVAILABLE
    # (the create path's orphaned-RESERVED discipline: FAILED is outside the
    # conflict status set, so a RESERVED device would be unbookable forever).
    for res in stuck:
        await _release_exclusive_devices_best_effort(
            res.id, list(res.device_ids), "provision_timeout_release"
        )
        # A timed-out reservation is now FAILED; archive its fork as the as-built
        # record (ADR 0006 Decision 5). A FAILED reservation still archives: the
        # fork records intended wiring even though provisioning never completed.
        await _archive_reservation_fork_best_effort(res.id)


async def _run_fork_archive_reconcile() -> None:
    """Reconcile ACTIVE forks against reservation state each tick (ADR 0006, ADR 0007).

    Three heals off ONE cabling fetch (ADR 0007 Decision 2: reuse the existing fetch,
    no second round-trip per tick):

    1. Archive (ADR 0006 Decision 5). Cabling keeps a fork ACTIVE until reservations
       archives it on teardown, so a crash between the terminal transition and the
       best-effort archive, or any pre-phase-3 fork, leaves an ACTIVE fork whose
       reservation is over: a zombie that false-contends for cross-reservation port
       claims. Forks whose reservation is terminal (COMPLETED/CANCELLED/FAILED) are
       archived best-effort. The first run archives every pre-phase-3 zombie (the
       one-time backfill, PR #353 notes).
    2. Wiring-staging heal (ADR 0007 Decision 2). A save that committed a new
       fork_version cabling-side but whose reservation.wiring_changed event never
       staged (reservations crashed in the save-then-stage gap) leaves the
       fork_wiring_ledger behind cabling's latest version. For each ACTIVE reservation
       whose cabling latest fork_version strictly exceeds its ledger
       (a missing ledger row counts as 0), a delta-less heal event is staged and the
       ledger advanced, atomically, exactly as the save path does.
    3. Missing-fork backstop (ADR 0009 phase 7). Initial provisioning is fork-driven,
       so an ACTIVE reservation with a parent topology but NO fork in cabling (its
       activation-time fork POST exhausted retries, or the process crashed before it)
       would otherwise stay unwired until the owner happens to open the bench
       (lazy-create). The sweep closes that gap: it fork-creates for exactly those
       reservations via the same idempotent create-then-stage helper activation uses,
       so the initial wiring_changed follows without user action.

    A reservation_id cabling reports that this DB does not know is logged and skipped,
    never touched blind: reservations is the lifecycle authority. The cabling fetch is
    non-fatal to the rest of the sweep: a failure returns early here and the loop also
    guards the call.
    """
    try:
        forks = await _fetch_active_forks()
    except Exception:
        logger.error(
            "Fork archive reconcile: could not fetch active forks from cabling",
            exc_info=True,
            extra={"action": "fork_reconcile_fetch_failed"},
        )
        return

    reservation_ids = [reservation_id for reservation_id, _ in forks]
    version_by_id = {reservation_id: version for reservation_id, version in forks}

    if reservation_ids:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Reservation.id, Reservation.status).where(
                    Reservation.id.in_(reservation_ids)
                )
            )
            status_by_id = {row.id: row.status for row in result}
    else:
        status_by_id = {}

    active_ids: list[uuid.UUID] = []
    for reservation_id in reservation_ids:
        status = status_by_id.get(reservation_id)
        if status is None:
            # Cabling holds an ACTIVE fork for a reservation this service does not
            # know. Do not touch it blind: log it for investigation and skip.
            logger.warning(
                "Fork archive reconcile: ACTIVE fork for unknown reservation %s; skipping",
                reservation_id,
                extra={
                    "action": "fork_reconcile_unknown_reservation",
                    "reservation_id": str(reservation_id),
                },
            )
            continue
        if status in TERMINAL_STATUSES:
            await _archive_reservation_fork_best_effort(reservation_id)
            logger.info(
                "Fork archive reconcile: archived fork for terminal reservation %s (%s)",
                reservation_id,
                status.value,
                extra={
                    "action": "fork_reconcile_archived",
                    "reservation_id": str(reservation_id),
                    "reservation_status": status.value,
                },
            )
        elif status == ReservationStatus.ACTIVE:
            active_ids.append(reservation_id)

    await _heal_wiring_staging(active_ids, version_by_id)
    await _backstop_missing_forks(set(version_by_id.keys()))


async def _heal_wiring_staging(
    active_ids: list[uuid.UUID], version_by_id: dict[uuid.UUID, int]
) -> None:
    """Stage a heal event for each ACTIVE fork whose version outran its ledger (ADR 0007).

    For each ACTIVE reservation, compares cabling's latest fork_version against the
    fork_wiring_ledger's last_staged_fork_version (a missing row counts as 0). A latest
    strictly greater than the ledger is a missed staging (the save-then-stage crash
    gap, Decision 2): stage a delta-less heal event (released/built None, the
    load-bearing full-reconcile marker) carrying that version and advance the ledger,
    atomically per reservation, exactly as the save path does. In-sync forks stage
    nothing.
    """
    if not active_ids:
        return
    async with AsyncSessionLocal() as db:
        for reservation_id in active_ids:
            cabling_version = version_by_id.get(reservation_id, 0)
            ledger = await db.get(ForkWiringLedger, reservation_id)
            last_staged = ledger.last_staged_fork_version if ledger is not None else 0
            if cabling_version <= last_staged:
                continue
            await stage_wiring_changed(
                db,
                reservation_id,
                cabling_version,
                released=None,
                built=None,
            )
            logger.info(
                "Wiring heal: staged reservation.wiring_changed v%s for reservation %s "
                "(ledger was %s)",
                cabling_version,
                reservation_id,
                last_staged,
                extra={
                    "action": "wiring_heal_staged",
                    "reservation_id": str(reservation_id),
                    "fork_version": cabling_version,
                },
            )


# ADR 0009 phase 7 hardening (issue #448 item 1): a give-up counter for the fork
# backstop below. A reservation whose fork create fails PERMANENTLY (e.g. its parent
# topology was deleted before any fork ever existed) would otherwise re-run the
# 3-attempt retry backoff every tick forever, delaying the same loop that also drives
# activation and completion. This is a per-process in-memory counter, not persisted:
# a process restart resetting it back to zero is acceptable, since the alternative
# (a schema change to persist give-up state for a best-effort backstop) is overkill,
# and a restart-triggered retry of a genuinely stuck reservation costs at most one
# more round of sweep attempts before giving up again.
_FORK_BACKSTOP_MAX_ATTEMPTS = 5
_fork_backstop_attempts: dict[uuid.UUID, int] = {}


async def _backstop_missing_forks(known_fork_ids: set[uuid.UUID]) -> None:
    """Fork-create for ACTIVE topology-carrying reservations cabling has no fork for.

    The ADR 0009 phase 7 backstop for the fork-creation-failure case: initial wiring
    is provisioned by the fork's activation-staged reservation.wiring_changed, so a
    reservation whose activation-time fork POST failed (exhausted retries, or a crash
    before the call) has no wiring source at all until its fork exists. Reservations
    with no parent topology are skipped, exactly as activation skips them (ADR 0001
    Decision 3 Case A: their fork is lazily created on first edit and starts empty,
    so there is no initial wiring to stage).

    Delegates to _create_reservation_fork_best_effort, so the create is the same
    idempotent cabling POST activation uses and the initial wiring_changed staging
    (ledger-guarded against double-staging a version) follows a success. Best-effort
    per reservation: one failure never blocks the rest, and the next tick retries.

    Give-up (issue #448 item 1): each reservation still missing a fork after a call
    here increments _fork_backstop_attempts; at _FORK_BACKSTOP_MAX_ATTEMPTS a warning
    is logged once and the reservation is skipped on every later tick (no create call,
    no further logging) until either the process restarts or the fork shows up via
    another path (lazy-create, a racing sweep). A reservation that now has a fork
    (present in known_fork_ids, including right after a successful create here) has
    its counter cleared, so a later unrelated failure starts a fresh count.

    Pruning (review follow-up): a reservation that accrues a nonzero counter and then
    leaves this tick's ACTIVE-with-topology row set entirely (the reservation ends,
    e.g. the user cancels a reservation whose parent topology was deleted, without the
    fork ever succeeding) is otherwise never visited again, so neither pop site above
    fires and its key would leak for the life of the process. Every key not present in
    this tick's row-id set is pruned up front, before the per-row loop, so the counter
    dict never outlives the reservations it tracks.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Reservation.id, Reservation.topology_id, Reservation.user_id).where(
                Reservation.status == ReservationStatus.ACTIVE,
                Reservation.topology_id.is_not(None),
            )
        )
        rows = result.all()

    current_ids = {row.id for row in rows}
    for stale_id in set(_fork_backstop_attempts.keys()) - current_ids:
        _fork_backstop_attempts.pop(stale_id, None)

    for row in rows:
        if row.id in known_fork_ids:
            _fork_backstop_attempts.pop(row.id, None)
            continue
        if _fork_backstop_attempts.get(row.id, 0) >= _FORK_BACKSTOP_MAX_ATTEMPTS:
            # Already gave up on this reservation; do not re-log or re-attempt.
            continue
        logger.info(
            "Fork backstop: ACTIVE reservation %s has a topology but no fork; creating",
            row.id,
            extra={
                "action": "fork_backstop_create",
                "reservation_id": str(row.id),
            },
        )
        created = await _create_reservation_fork_best_effort(
            row.id, row.topology_id, created_by=str(row.user_id)
        )
        if created:
            _fork_backstop_attempts.pop(row.id, None)
            continue

        attempts = _fork_backstop_attempts.get(row.id, 0) + 1
        _fork_backstop_attempts[row.id] = attempts
        if attempts >= _FORK_BACKSTOP_MAX_ATTEMPTS:
            logger.warning(
                "Fork backstop: giving up on reservation %s after %d failed attempts; "
                "will not retry until a process restart or the fork appears via another path",
                row.id,
                attempts,
                extra={
                    "action": "fork_backstop_give_up",
                    "reservation_id": str(row.id),
                    "attempts": attempts,
                },
            )


# Per-tick cap on pending-prune retries, bounding the sweep's cabling fan-out the
# same way the active-fork fetch bounds the other heals. A backlog past the cap is
# picked up on later ticks (ordered by updated_at, so the oldest converge first).
_PENDING_PRUNE_BATCH = 20


async def _run_pending_prune_reconcile() -> None:
    """Retry pending device-removal fork prunes each tick (issue #462).

    The REMOVE half's standing reconciler, the partner of the archive heal, the
    wiring-staging heal, and the missing-fork backstop: the device-set PATCH records
    removed device ids in pending_fork_prune_device_ids atomically with the edit, so
    a prune that failed its immediate best-effort attempts (cabling outage, 5xx, or a
    crash between the commit and the call) stays durably visible here. Each tick
    retries up to _PENDING_PRUNE_BATCH marked reservations with ONE attempt each (the
    PATCH fast path already ran the backoff; a persistent outage retries next tick,
    the same forever-until-converged posture as the wiring heal). The prune replay is
    idempotent, and success clears exactly the retried ids from the marker
    (set-difference under a row lock, so ids a concurrent PATCH unioned in survive).

    A reservation that went terminal meanwhile drops its marker without a prune:
    terminal teardown releases its wiring from execution's intended ledgers and the
    fork archive settles the port claims, so there is nothing left for a prune to do
    (cabling would answer the ARCHIVED 409 anyway). ACTIVE is the only status that
    writes the marker, so non-ACTIVE here means terminal.
    """
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    Reservation.id,
                    Reservation.status,
                    Reservation.pending_fork_prune_device_ids,
                )
                .where(Reservation.pending_fork_prune_device_ids.is_not(None))
                .order_by(Reservation.updated_at)
                .limit(_PENDING_PRUNE_BATCH)
            )
        ).all()

    for row in rows:
        pending = row.pending_fork_prune_device_ids or []
        if row.status != ReservationStatus.ACTIVE:
            await _clear_pending_fork_prune(row.id, pending)
            logger.info(
                "Pending prune reconcile: reservation %s is %s; teardown owns the release",
                row.id,
                row.status.value,
                extra={
                    "action": "pending_prune_terminal_cleared",
                    "reservation_id": str(row.id),
                },
            )
            continue
        device_ids: list[uuid.UUID] = []
        bad_entries: list = []
        for raw in pending:
            try:
                device_ids.append(uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                # Defensive: the marker is written from validated UUIDs, so a bad
                # entry is corruption; clear it rather than wedging the reconciler.
                bad_entries.append(raw)
        if bad_entries:
            await _clear_pending_fork_prune(row.id, bad_entries)
        if not device_ids:
            continue
        logger.info(
            "Pending prune reconcile: retrying fork prune for reservation %s (%d devices)",
            row.id,
            len(device_ids),
            extra={
                "action": "pending_prune_retry",
                "reservation_id": str(row.id),
            },
        )
        await _prune_removed_devices_from_fork_best_effort(row.id, device_ids, attempts=1)


def _dynamic_requests_classify_payload(
    dynamic_requests: list[ReservationDynamicRequest],
) -> list[dict] | None:
    """Group a reservation's dynamic request rows into {template_id, count}.

    Each ReservationDynamicRequest row is one requested instance (issue #32
    deliberately has no per-row count, so N rows of the same template_id means
    N instances); the classify-purpose contract wants one entry per distinct
    template with its count. None (not an empty list) for a physical-only
    reservation, matching the contract's `[...] | null`.
    """
    if not dynamic_requests:
        return None
    counts: dict[str, int] = {}
    order: list[str] = []
    for dr in dynamic_requests:
        tid = str(dr.template_id)
        if tid not in counts:
            order.append(tid)
        counts[tid] = counts.get(tid, 0) + 1
    return [{"template_id": tid, "count": counts[tid]} for tid in order]


async def _bump_purpose_classify_attempts(reservation_id: uuid.UUID) -> None:
    """Increment purpose_classify_attempts for one row, in its own transaction."""
    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None:
            return
        res.purpose_classify_attempts += 1
        await db.commit()


async def _classify_purpose_one(reservation_id: uuid.UUID) -> str:
    """Classify one reservation's purpose via the AI orchestrator; never raises.

    Returns "ok" (a suggestion was stored, or the row was already resolved by
    a concurrent writer), "feature_off" (the orchestrator answered 403,
    meaning AI_PURPOSE_CLASSIFICATION_ENABLED is off, or answered 404,
    meaning the running orchestrator image predates POST
    /internal/classify-purpose (a mixed-version deployment where only
    reservations has been upgraded); either way the row is left untouched,
    no attempt counted), or "failed" (any other non-200, a bad body, or a
    transport error/timeout; purpose_classify_attempts is incremented). Each
    outcome that mutates the row does so in its own session/commit, so one
    row's failure never affects another row in the same batch.
    """
    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None or res.purpose_suggestion is not None:
            # Already resolved by a concurrent writer (another instance's
            # sweep tick, or an admin action) since this row was selected for
            # the batch: nothing to do.
            return "ok"
        payload = {
            "reservation_id": str(res.id),
            "categories": list(settings.purpose_categories),
            "purpose": res.purpose,
            "user_id": str(res.user_id),
            "device_ids": [str(d) for d in res.device_ids],
            "topology_id": str(res.topology_id) if res.topology_id else None,
            "dynamic_requests": _dynamic_requests_classify_payload(res.dynamic_requests),
            "start_time": res.start_time.isoformat(),
            "end_time": res.end_time.isoformat(),
            "status": res.status.value,
        }

    try:
        resp = await call_service(
            settings.ai_orchestrator_service_url,
            "POST",
            "/internal/classify-purpose",
            json_body=payload,
            timeout=settings.purpose_classify_timeout_seconds,
            auth=InternalTokenAuth(token=settings.internal_api_token),
        )
    except Exception:
        logger.warning(
            "Purpose classify reconcile: call to the orchestrator failed for %s",
            reservation_id,
            exc_info=True,
            extra={
                "action": "purpose_classify_call_failed",
                "reservation_id": str(reservation_id),
            },
        )
        await _bump_purpose_classify_attempts(reservation_id)
        return "failed"

    if resp.status_code in (403, 404):
        # 403 means AI_PURPOSE_CLASSIFICATION_ENABLED is off on the
        # orchestrator; 404 means the running orchestrator image predates
        # this endpoint entirely (a mixed-version deployment mid-upgrade, or
        # a stack where only reservations was updated). Both are "not
        # available yet", not a per-row failure, so neither counts an
        # attempt; the two are kept distinguishable in the log message so an
        # operator can tell a flag flip from a stale image.
        if resp.status_code == 403:
            logger.info(
                "Purpose classify reconcile: the orchestrator answered 403 for %s "
                "(AI_PURPOSE_CLASSIFICATION_ENABLED is off there); treating this as "
                "feature-off for this tick",
                reservation_id,
                extra={
                    "action": "purpose_classify_feature_off",
                    "reservation_id": str(reservation_id),
                    "status_code": 403,
                },
            )
        else:
            logger.info(
                "Purpose classify reconcile: the orchestrator answered 404 for %s "
                "(it does not expose POST /internal/classify-purpose yet); treating "
                "this as feature-off for this tick",
                reservation_id,
                extra={
                    "action": "purpose_classify_feature_off",
                    "reservation_id": str(reservation_id),
                    "status_code": 404,
                },
            )
        return "feature_off"

    if resp.status_code != 200:
        logger.warning(
            "Purpose classify reconcile: orchestrator returned %s for %s",
            resp.status_code,
            reservation_id,
            extra={
                "action": "purpose_classify_bad_status",
                "reservation_id": str(reservation_id),
                "status_code": resp.status_code,
            },
        )
        await _bump_purpose_classify_attempts(reservation_id)
        return "failed"

    try:
        suggestion = resp.json()
    except ValueError:
        logger.warning(
            "Purpose classify reconcile: unparseable 200 body for %s",
            reservation_id,
            extra={"action": "purpose_classify_bad_body", "reservation_id": str(reservation_id)},
        )
        await _bump_purpose_classify_attempts(reservation_id)
        return "failed"

    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None:
            return "ok"
        res.purpose_suggestion = suggestion
        res.purpose_suggested_at = datetime.now(timezone.utc)
        await db.commit()
    logger.info(
        "Purpose classify reconcile: stored a suggestion for %s",
        reservation_id,
        extra={"action": "purpose_classify_stored", "reservation_id": str(reservation_id)},
    )
    return "ok"


async def _run_purpose_classify_reconcile() -> None:
    """End-of-reservation background purpose classification (issue #646 phase 2,
    ADR 0013 point 8's second pass).

    Each tick selects up to `purpose_classify_batch_size` rows where
    purpose_classify_requested_at is set, purpose_suggestion is still null, and
    purpose_classify_attempts is under the cap, oldest requested first (so a
    backfill of old rows drains before newer terminal reservations queue
    behind it), and classifies each in turn via _classify_purpose_one.

    A "feature_off" outcome (the orchestrator answered 403, meaning
    AI_PURPOSE_CLASSIFICATION_ENABLED is off there, or answered 404, meaning
    the running orchestrator image does not expose POST
    /internal/classify-purpose yet, e.g. a mixed-version deployment mid
    upgrade) ends the WHOLE tick immediately without touching any row,
    including ones later in the batch: there is no point spending N more
    round trips confirming the same not-available condition, and none of
    them should count as a consumed attempt. Any other failure only affects
    its own row; the loop continues to the next candidate. This function
    never raises: every per-row failure is caught inside
    _classify_purpose_one.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Reservation.id)
            .where(
                and_(
                    Reservation.purpose_classify_requested_at.is_not(None),
                    Reservation.purpose_suggestion.is_(None),
                    Reservation.purpose_classify_attempts < settings.purpose_classify_max_attempts,
                )
            )
            .order_by(Reservation.purpose_classify_requested_at)
            .limit(settings.purpose_classify_batch_size)
        )
        reservation_ids = [row[0] for row in result.all()]

    for reservation_id in reservation_ids:
        outcome = await _classify_purpose_one(reservation_id)
        if outcome == "feature_off":
            # _classify_purpose_one already logged the specific reason (403
            # vs 404); this is just the tick-level "stopped here" note.
            logger.info(
                "Purpose classify reconcile: orchestrator classification is not "
                "available; ending this tick",
                extra={"action": "purpose_classify_feature_off_tick_end"},
            )
            break


async def expiration_loop(interval_seconds: int = 60) -> None:
    """Run expiration cycles forever at the given interval.

    Each tick runs the state-machine cycle (activate/complete) and then the
    upcoming-expiry reminder cycle. Both cycles stage their NATS events
    (reservation.completed, reservation.created, reservation.expiring_soon) into
    the outbox in the same transaction as the state change (issue #21); the
    outbox relay publishes them, so the loop no longer needs a NATS connection.
    """
    logger.info("Expiration loop started, interval=%ds", interval_seconds)
    while True:
        try:
            await _run_expiration_cycle()
        except Exception:
            logger.error("Expiration cycle failed", exc_info=True)
        try:
            await _run_reminder_cycle()
        except Exception:
            logger.error("Expiry reminder cycle failed", exc_info=True)
        try:
            await _run_fork_archive_reconcile()
        except Exception:
            logger.error("Fork archive reconcile cycle failed", exc_info=True)
        try:
            await _run_pending_prune_reconcile()
        except Exception:
            logger.error("Pending fork-prune reconcile cycle failed", exc_info=True)
        try:
            await _run_purpose_classify_reconcile()
        except Exception:
            logger.error("Purpose classify reconcile cycle failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
