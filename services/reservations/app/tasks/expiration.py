"""Background task that auto-activates and auto-completes reservations."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from herd_common.outbox import enqueue_event
from herd_common.retry import retry_with_backoff
from sqlalchemy import and_, exists, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
)
from app.services.reservation_service import (
    _archive_reservation_fork_best_effort,
    _claim_provision_transition,
    _create_reservation_fork_best_effort,
    _fetch_active_fork_reservation_ids,
    _fetch_devices_best_effort,
    _provision_requested_event,
    _release_exclusive_devices_best_effort,
    _reservation_created_event,
    _reservation_failed_event,
    _update_device_statuses,
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
    """Archive forks whose reservation reached a terminal status (ADR 0006 Decision 5).

    Standing reconciler AND the required one-time backfill (PR #353 notes). Cabling
    keeps a fork ACTIVE until reservations archives it on teardown, so two things
    leave an ACTIVE fork whose reservation is over: a crash between the terminal
    transition and the best-effort archive, and any pre-phase-3 fork created before
    the teardown-archive wiring existed. Such a fork is a zombie that false-contends
    for cross-reservation port claims. Each tick this fetches cabling's ACTIVE-fork
    reservation_ids, keeps those whose reservation is terminal
    (COMPLETED/CANCELLED/FAILED), and archives each best-effort. The first run
    archives every pre-phase-3 zombie, which is the backfill.

    A reservation_id cabling reports that this DB does not know is logged and
    skipped, never archived blind: reservations is the lifecycle authority, and only
    a reservation it can read attests that its fork should be frozen. A reservation
    still non-terminal (ACTIVE and legitimately in use) is left alone. The cabling
    fetch is non-fatal to the rest of the sweep: a failure returns early here and the
    loop also guards the call.
    """
    try:
        reservation_ids = await _fetch_active_fork_reservation_ids()
    except Exception:
        logger.error(
            "Fork archive reconcile: could not fetch active forks from cabling",
            exc_info=True,
            extra={"action": "fork_reconcile_fetch_failed"},
        )
        return

    if not reservation_ids:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Reservation.id, Reservation.status).where(Reservation.id.in_(reservation_ids))
        )
        status_by_id = {row.id: row.status for row in result}

    for reservation_id in reservation_ids:
        status = status_by_id.get(reservation_id)
        if status is None:
            # Cabling holds an ACTIVE fork for a reservation this service does not
            # know. Do not archive blind: log it for investigation and skip.
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
        await asyncio.sleep(interval_seconds)
