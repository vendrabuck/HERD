"""Background task that auto-activates and auto-completes reservations."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.reservation import Reservation, ReservationStatus
from app.services.reservation_service import (
    _fetch_devices_best_effort,
    _publish_nats_event,
    _update_device_statuses,
)

logger = logging.getLogger(__name__)

EXPIRING_SOON_SUBJECT = "herd.reservations.expiring_soon"
COMPLETED_SUBJECT = "herd.reservations.completed"


async def _run_reminder_cycle(nats_conn) -> None:
    """Emit one reservation.expiring_soon event per reservation in the lead window.

    Selects ACTIVE reservations whose end_time is in the future but within
    `expiry_reminder_lead_seconds` of now and that have not been reminded yet
    (expiry_reminder_sent_at is null). Stamps the timestamp inside the same
    transaction so a row is claimed before its event is published; this dedupes
    the reminder per reservation across ticks. A lead window of 0 disables the
    reminder entirely.

    The event is published after the row is committed. If the publish fails it
    is logged (never raised); the reminder is not re-attempted because the row
    is already stamped, matching the at-most-once intent (a missed reminder is
    preferable to a duplicate, and the user still gets the completion event).
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
        # Snapshot the payload fields while the row (and its eager-loaded
        # devices) is attached, then stamp and commit before publishing.
        pending: list[dict] = []
        for res in due:
            pending.append(
                {
                    "event": "reservation.expiring_soon",
                    "reservation_id": str(res.id),
                    "user_id": str(res.user_id),
                    "device_ids": [str(d) for d in res.device_ids],
                    "end_time": res.end_time.isoformat(),
                }
            )
            res.expiry_reminder_sent_at = now
        await db.commit()

    for event in pending:
        logger.info(
            "Emitting expiring_soon reminder for reservation %s",
            event["reservation_id"],
            extra={
                "action": "reservation_expiring_soon",
                "reservation_id": event["reservation_id"],
            },
        )
        await _publish_nats_event(nats_conn, EXPIRING_SOON_SUBJECT, event)


async def _run_expiration_cycle(nats_conn=None) -> None:
    """Single expiration cycle: activate pending, complete expired.

    Auto-completion is the normal end-of-life path for a reservation (most end by
    reaching end_time, not by manual release). For each reservation it completes,
    this emits a reservation.completed event on the same subject and with the same
    payload shape as the manual release path (release_reservation), so the
    execution service deprovisions (L1 disconnect, L2 VLAN teardown) and
    notifications renders the completion. Without it the devices are flipped to
    AVAILABLE in inventory while their config stays wired, letting a new
    reservation be booked on top of stale, never-torn-down config.

    The event is published after the transaction commits, once per completed
    reservation. Publishing is best-effort (errors are logged, never raised by
    _publish_nats_event); if nats_conn is None the publish is a no-op, consistent
    with the rest of the service treating NATS as non-fatal.
    """
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
                "Auto-activated reservation %s",
                res.id,
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
        # Snapshot the completion payload while the row (and its eager-loaded
        # device_ids) is attached. Mirror the manual release_reservation payload
        # exactly: same subject, event name, and fields, so execution and
        # notifications consumers handle an auto-expiry identically to a manual
        # release. device_ids is the reservation's full set (every device in the
        # topology), not just the exclusive subset released in inventory below.
        completed_events: list[dict] = []
        for res in expired:
            res.status = ReservationStatus.COMPLETED
            completed_events.append(
                {
                    "event": "reservation.completed",
                    "reservation_id": str(res.id),
                    "user_id": str(res.user_id),
                    "device_ids": [str(d) for d in res.device_ids],
                    "topology_id": str(res.topology_id) if res.topology_id else None,
                    "topology_type": res.topology_type.value,
                }
            )
            logger.info(
                "Auto-completed reservation %s",
                res.id,
                extra={"action": "auto_complete", "reservation_id": str(res.id)},
            )

        await db.commit()

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

    # Emit a completion event per auto-completed reservation, after the commit,
    # so consumers tear down the topology (execution deprovision/disconnect) and
    # render the completion (notifications). Same helper the manual release path
    # uses; best-effort, never raised.
    for event in completed_events:
        await _publish_nats_event(nats_conn, COMPLETED_SUBJECT, event)


async def expiration_loop(interval_seconds: int = 60, nats_conn=None) -> None:
    """Run expiration cycles forever at the given interval.

    Each tick runs the state-machine cycle (activate/complete) and then the
    upcoming-expiry reminder cycle. Both cycles need the NATS connection: the
    expiration cycle publishes reservation.completed for auto-completed
    reservations and the reminder cycle publishes reservation.expiring_soon. If
    NATS is unavailable (nats_conn is None) the publishes are no-ops, consistent
    with the rest of the service treating NATS as non-fatal.
    """
    logger.info("Expiration loop started, interval=%ds", interval_seconds)
    while True:
        try:
            await _run_expiration_cycle(nats_conn)
        except Exception:
            logger.error("Expiration cycle failed", exc_info=True)
        try:
            await _run_reminder_cycle(nats_conn)
        except Exception:
            logger.error("Expiry reminder cycle failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
