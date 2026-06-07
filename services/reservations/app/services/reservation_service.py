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
from datetime import datetime, timezone

import httpx
from herd_common.retry import retry_with_backoff
from sqlalchemy import and_, exists, false, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.reservation import (
    Reservation,
    ReservationDevice,
    ReservationStatus,
    TopologyType,
)
from app.schemas.reservation import ReservationCreate, ReservationUpdate

logger = logging.getLogger(__name__)


async def _fetch_devices(device_ids: list[uuid.UUID], token: str) -> list[dict]:
    """Fetch device info from Inventory service concurrently. All-or-nothing:
    if any device fetch fails, the whole call raises. Used by create/update
    paths that must validate every device before committing.
    """
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


async def _fetch_devices_best_effort(
    device_ids: list[uuid.UUID],
) -> list[dict | BaseException]:
    """Fetch device info concurrently via the internal-token route, returning
    per-device successes or the exception that interrupted that device's
    fetch. Used by cancel/release/auto-expire which all act as the system
    rather than as the user (the user's permission was checked upstream when
    they invoked the cancel/release; the actual device-release work is
    service-to-service).
    """
    if not settings.internal_api_token:
        # Without an internal token, callers should fall back to treating
        # every device as exclusive. Return an empty list to keep the type
        # consistent; the callers' zip() over (ids, results) handles short
        # lists by yielding no entries, which the caller treats as "no info,
        # assume exclusive".
        return [
            RuntimeError("internal_api_token not configured; cannot fetch device info")
            for _ in device_ids
        ]

    async with httpx.AsyncClient() as client:

        async def fetch_one(device_id: uuid.UUID) -> dict:
            resp = await client.get(
                f"{settings.inventory_service_url}/devices/{device_id}/internal",
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
            if resp.status_code == 404:
                raise ValueError(f"Device {device_id} not found in inventory")
            resp.raise_for_status()
            return resp.json()

        return await asyncio.gather(*[fetch_one(did) for did in device_ids], return_exceptions=True)


async def _validate_topology_connectivity(topology_id: uuid.UUID) -> None:
    """Reject the reservation when the referenced topology has unreachable edges.

    The cabling service walks the physical Connection graph and returns a list of
    edges with no path between endpoints (e.g., devices in physically isolated
    fabrics). Server-side enforcement is the authority; the editor's red lines
    are informational.

    Authenticated as a service-to-service call via X-Internal-Token rather than
    forwarding the booking user's JWT: the booking user does not necessarily
    own the topology they're reserving, so JWT-forward would 403 against the
    cabling RBAC check.
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{settings.cabling_service_url}/topologies/{topology_id}/validate/internal",
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to contact cabling service: {exc}") from exc

    if resp.status_code == 404:
        # Topology was deleted between selection and submit; let downstream
        # checks decide. Treat as no validation rather than a hard failure.
        return
    if resp.status_code >= 400:
        raise RuntimeError(f"Cabling validation returned {resp.status_code}: {resp.text}")

    body = resp.json()
    if body.get("valid"):
        return

    invalid = body.get("invalid_edges") or []
    summaries = []
    for entry in invalid[:5]:
        edge_id = entry.get("edge_id") or "?"
        reason = entry.get("reason") or "invalid"
        summaries.append(f"{edge_id} ({reason})")
    extra = "" if len(invalid) <= 5 else f" and {len(invalid) - 5} more"
    raise ValueError(
        "Topology has unreachable edges in the cabling graph: " + ", ".join(summaries) + extra
    )


async def _check_conflicts(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    start_time: datetime,
    end_time: datetime,
    exclude_id: uuid.UUID | None = None,
    exclusive_device_ids: set[str] | None = None,
) -> list[uuid.UUID]:
    """
    Returns a list of device_ids that have conflicting reservations in the given window.
    Conflict = any ACTIVE/PENDING/PENDING_PROVISION reservation overlapping [start_time, end_time).
    PENDING_PROVISION is included so a second create during the provisioning window is rejected.
    Only exclusive devices are checked when exclusive_device_ids is provided.
    """
    # Only check exclusive devices when the set is provided.
    requested_str = {str(d) for d in device_ids}
    if exclusive_device_ids is not None:
        requested_str = requested_str & exclusive_device_ids
    if not requested_str:
        return []
    requested_uuids = [uuid.UUID(d) for d in requested_str]

    # Indexed join over reservation_devices: a device conflicts when it belongs to
    # any ACTIVE/PENDING/PENDING_PROVISION reservation overlapping the window.
    # PENDING_PROVISION is included so a second create during the provisioning
    # window is rejected; the join rows commit atomically with their reservation.
    query = (
        select(ReservationDevice.device_id)
        .join(Reservation, Reservation.id == ReservationDevice.reservation_id)
        .where(
            ReservationDevice.device_id.in_(requested_uuids),
            Reservation.status.in_(
                [
                    ReservationStatus.ACTIVE,
                    ReservationStatus.PENDING,
                    ReservationStatus.PENDING_PROVISION,
                ]
            ),
            Reservation.start_time < end_time,
            Reservation.end_time > start_time,
        )
        .distinct()
    )
    if exclude_id:
        query = query.where(Reservation.id != exclude_id)

    result = await db.execute(query)
    return list(result.scalars().all())


async def _publish_nats_event(nc, subject: str, event: dict) -> None:
    """Publish a NATS event using the provided connection. Errors are logged, never raised."""
    if nc is None:
        return
    try:
        js = nc.jetstream()
        await js.publish(
            subject,
            json.dumps(event, default=str).encode(),
        )
    except Exception:
        logger.error("Failed to publish NATS event: %s", event.get("event"), exc_info=True)


async def _update_device_statuses(
    device_ids: list[uuid.UUID],
    status: str,
    *,
    raise_on_failure: bool = False,
) -> None:
    """Update device statuses in the inventory service via the internal token.

    Default behavior is best-effort: errors are logged and never raised. Pass
    raise_on_failure=True to raise RuntimeError if any device fails to update;
    callers on the create/cancel/release paths wrap this in retry_with_backoff
    and convert exhausted retries into a structured failure log.
    """
    if not settings.internal_api_token:
        return

    async with httpx.AsyncClient() as client:

        async def update_one(device_id: uuid.UUID) -> None:
            resp = await client.post(
                f"{settings.inventory_service_url}/devices/{device_id}/status",
                json={"status": status},
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
            resp.raise_for_status()

        results = await asyncio.gather(
            *[update_one(did) for did in device_ids], return_exceptions=True
        )

    failed = [(did, exc) for did, exc in zip(device_ids, results) if isinstance(exc, BaseException)]
    for did, exc in failed:
        logger.error("Failed to update device %s status to %s: %s", did, status, exc, exc_info=exc)

    if failed and raise_on_failure:
        raise RuntimeError(
            f"inventory status update failed for {len(failed)} device(s): "
            f"{[str(d) for d, _ in failed]}"
        )


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
    username: str = "",
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

    # 2b. If a topology is referenced, validate every edge maps to a real path
    # in the cabling graph. Reservations without a topology are unaffected.
    if data.topology_id is not None:
        await _validate_topology_connectivity(data.topology_id)

    # Partition devices into exclusive vs non-exclusive
    exclusive_ids = {str(d["id"]) for d in devices if d.get("exclusive", True)}
    non_exclusive_ids = {str(d["id"]) for d in devices if not d.get("exclusive", True)}

    # 3. Check availability: exclusive devices must be AVAILABLE;
    #    non-exclusive devices accept AVAILABLE or RESERVED
    bad_exclusive = [
        d["name"] for d in devices if str(d["id"]) in exclusive_ids and d["status"] != "AVAILABLE"
    ]
    bad_non_exclusive = [
        d["name"]
        for d in devices
        if str(d["id"]) in non_exclusive_ids and d["status"] not in ("AVAILABLE", "RESERVED")
    ]
    unavailable = bad_exclusive + bad_non_exclusive
    if unavailable:
        raise ValueError(f"The following devices are not available: {', '.join(unavailable)}")

    # 4. Acquire advisory locks to prevent concurrent conflicting reservations
    await _acquire_device_locks(db, data.device_ids)

    # 5. Check time-window conflicts (only for exclusive devices)
    conflicting = await _check_conflicts(
        db,
        data.device_ids,
        data.start_time,
        data.end_time,
        exclusive_device_ids=exclusive_ids,
    )
    if conflicting:
        raise LookupError(
            f"Time conflict: devices {[str(d) for d in conflicting]} already reserved "
            f"in the requested window"
        )

    # 6. Create reservation as PENDING_PROVISION and commit. The row is visible to
    #    _check_conflicts (step 5 in concurrent creates) immediately, which closes the
    #    double-booking race while we talk to inventory below.
    exclusive_uuid_ids = [d for d in data.device_ids if str(d) in exclusive_ids]
    initial_status = (
        ReservationStatus.PENDING_PROVISION if exclusive_uuid_ids else ReservationStatus.ACTIVE
    )
    reservation = Reservation(
        user_id=user_id,
        owner_name=username,
        device_ids=list(data.device_ids),
        topology_id=data.topology_id,
        topology_type=topology_type,
        purpose=data.purpose,
        start_time=data.start_time,
        end_time=data.end_time,
        status=initial_status,
    )
    db.add(reservation)
    # The cascaded reservation_devices rows flush in this same commit, so the
    # PENDING_PROVISION reservation and its device memberships become visible
    # together: exactly what _check_conflicts needs to reject a concurrent create.
    await db.commit()
    # expire_on_commit=False keeps the eager-loaded devices collection populated
    # across the commit; a full refresh reloads the scalar columns.
    await db.refresh(reservation)

    logger.info(
        "Reservation created: %s",
        reservation.id,
        extra={
            "action": "reservation_create",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
            "initial_status": initial_status.value,
        },
    )

    # 7. Mark exclusive devices as RESERVED in inventory with retry; non-exclusive
    #    devices stay AVAILABLE so no inventory call is needed. On exhausted retries
    #    the reservation is flipped to FAILED and no NATS event is emitted.
    if exclusive_uuid_ids:
        try:
            await retry_with_backoff(
                lambda: _update_device_statuses(
                    exclusive_uuid_ids, "RESERVED", raise_on_failure=True
                ),
                attempts=3,
                initial_delay=0.5,
                factor=2.0,
                max_delay=5.0,
            )
        except Exception as exc:
            reservation.status = ReservationStatus.FAILED
            await db.commit()
            await db.refresh(reservation)
            logger.error(
                "Reservation provisioning failed: %s",
                reservation.id,
                extra={
                    "action": "reservation_provision_failed",
                    "reservation_id": str(reservation.id),
                    "user_id": str(user_id),
                },
                exc_info=exc,
            )
            raise RuntimeError(
                f"Failed to reserve devices in inventory after retries: {exc}"
            ) from exc

        reservation.status = ReservationStatus.ACTIVE
        await db.commit()
        await db.refresh(reservation)

    # 8. Emit NATS event (only on successful provisioning)
    await _publish_nats_event(
        nats_conn,
        "herd.reservations.created",
        {
            "event": "reservation.created",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
            "device_ids": [str(d) for d in data.device_ids],
            "topology_id": str(data.topology_id) if data.topology_id else None,
            "topology_type": topology_type.value,
            "start_time": data.start_time.isoformat(),
            "end_time": data.end_time.isoformat(),
        },
    )

    return reservation


async def list_user_reservations(
    db: AsyncSession, user_id: uuid.UUID, skip: int = 0, limit: int = 50
) -> tuple[list[Reservation], int]:
    base = select(Reservation).where(Reservation.user_id == user_id)

    from sqlalchemy import func

    count_query = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        base.order_by(Reservation.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all()), total


async def list_calendar_reservations(
    db: AsyncSession,
    range_start: datetime,
    range_end: datetime,
    status_filter: list[ReservationStatus] | None = None,
    device_id: uuid.UUID | None = None,
    visible_device_ids: set[str] | None = None,
) -> list[Reservation]:
    """Return all reservations overlapping the given time range (cross-user).
    If visible_device_ids is provided, only include reservations where ALL devices are visible."""
    query = select(Reservation).where(
        and_(
            Reservation.start_time < range_end,
            Reservation.end_time > range_start,
        )
    )
    if status_filter:
        query = query.where(Reservation.status.in_(status_filter))

    # Device filters as indexed EXISTS subqueries over reservation_devices.
    # Portable across Postgres and SQLite, so there is no dialect split.
    if device_id is not None:
        query = query.where(
            exists().where(
                and_(
                    ReservationDevice.reservation_id == Reservation.id,
                    ReservationDevice.device_id == device_id,
                )
            )
        )

    if visible_device_ids is not None:
        if not visible_device_ids:
            # The user can see no devices, so no reservation has all-visible devices.
            query = query.where(false())
        else:
            visible_uuids = [uuid.UUID(v) for v in visible_device_ids]
            # Include a reservation only when NO device on it falls outside the
            # visible set (i.e. device_ids is a subset of visible_device_ids).
            query = query.where(
                ~exists().where(
                    and_(
                        ReservationDevice.reservation_id == Reservation.id,
                        ReservationDevice.device_id.notin_(visible_uuids),
                    )
                )
            )

    query = query.order_by(Reservation.start_time.asc())

    result = await db.execute(query)
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


async def update_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ReservationUpdate,
    token: str = "",
    nats_conn=None,
) -> Reservation | None:
    """Update an ACTIVE or PENDING reservation (end_time, purpose)."""
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        return None

    if reservation.status not in (ReservationStatus.ACTIVE, ReservationStatus.PENDING):
        raise ValueError(f"Cannot update a {reservation.status.value} reservation")

    end_time_changed = False
    if data.end_time is not None:
        # Normalize timezone awareness for comparison (SQLite returns naive datetimes)
        new_end = data.end_time.replace(tzinfo=None) if data.end_time.tzinfo else data.end_time
        start = (
            reservation.start_time.replace(tzinfo=None)
            if reservation.start_time.tzinfo
            else reservation.start_time
        )
        old_end = (
            reservation.end_time.replace(tzinfo=None)
            if reservation.end_time.tzinfo
            else reservation.end_time
        )

        if new_end <= start:
            raise ValueError("end_time must be after start_time")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if new_end <= now:
            raise ValueError("end_time must be in the future")

        # If extending, check for conflicts in the extended window
        if new_end > old_end:
            device_ids = [uuid.UUID(str(d)) for d in reservation.device_ids]
            try:
                devices = await _fetch_devices(device_ids, token)
                exclusive_ids = {str(d["id"]) for d in devices if d.get("exclusive", True)}
            except Exception:
                exclusive_ids = {str(d) for d in device_ids}

            conflicting = await _check_conflicts(
                db,
                device_ids,
                reservation.end_time,
                data.end_time,
                exclude_id=reservation.id,
                exclusive_device_ids=exclusive_ids,
            )
            if conflicting:
                raise LookupError(
                    f"Time conflict: devices {[str(d) for d in conflicting]} already reserved "
                    f"in the extended window"
                )

        end_time_changed = new_end != old_end
        reservation.end_time = data.end_time

    if data.purpose is not None:
        reservation.purpose = data.purpose

    added_ids: list[uuid.UUID] = []
    removed_ids: list[uuid.UUID] = []

    if data.device_ids is not None:
        old_set = {str(d) for d in reservation.device_ids}
        new_set = {str(d) for d in data.device_ids}

        added_ids = [d for d in data.device_ids if str(d) not in old_set]
        removed_ids = [uuid.UUID(d) for d in old_set if d not in new_set]

        if added_ids or removed_ids:
            # Fetch all new devices to validate
            try:
                new_devices = await _fetch_devices(data.device_ids, token)
            except ValueError as exc:
                raise exc
            except Exception as exc:
                raise RuntimeError(f"Failed to contact inventory service: {exc}") from exc

            # Validate topology_type uniformity
            topology_types = {d["topology_type"] for d in new_devices}
            if len(topology_types) > 1:
                raise ValueError(
                    f"All devices must share the same topology type. "
                    f"Found: {', '.join(topology_types)}"
                )

            # Re-validate topology connectivity when the device set changes on a
            # reservation that references a topology. The cabling service walks the
            # canvas edges against the physical Connection graph and raises if any
            # edge is unreachable, so an edit that strands part of the topology is
            # rejected here rather than silently breaking a live reservation. Runs
            # before any inventory status mutation below, so a failure aborts with
            # no side effects to unwind. Reservations without a topology are
            # unaffected (this mirrors the create-path check).
            if reservation.topology_id is not None:
                await _validate_topology_connectivity(reservation.topology_id)

            # Check added exclusive devices are available and have no conflicts
            if added_ids:
                added_devices = [
                    d for d in new_devices if str(d["id"]) in {str(a) for a in added_ids}
                ]
                added_exclusive = {str(d["id"]) for d in added_devices if d.get("exclusive", True)}

                bad = [
                    d["name"]
                    for d in added_devices
                    if str(d["id"]) in added_exclusive and d["status"] != "AVAILABLE"
                ]
                if bad:
                    raise ValueError(f"The following devices are not available: {', '.join(bad)}")

                await _acquire_device_locks(db, added_ids)

                # Use reservation end_time (possibly updated above)
                check_end = data.end_time if data.end_time is not None else reservation.end_time
                # Normalize timezone for SQLite compatibility
                now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                start_naive = (
                    reservation.start_time.replace(tzinfo=None)
                    if reservation.start_time.tzinfo
                    else reservation.start_time
                )
                check_start = now_naive if now_naive > start_naive else reservation.start_time

                conflicting = await _check_conflicts(
                    db,
                    added_ids,
                    check_start,
                    check_end,
                    exclude_id=reservation.id,
                    exclusive_device_ids=added_exclusive,
                )
                if conflicting:
                    raise LookupError(
                        f"Time conflict: devices {[str(d) for d in conflicting]} already reserved"
                    )

                # Mark added exclusive devices as RESERVED
                added_exclusive_uuids = [d for d in added_ids if str(d) in added_exclusive]
                if added_exclusive_uuids:
                    await _update_device_statuses(added_exclusive_uuids, "RESERVED")

            # Release removed exclusive devices
            if removed_ids:
                try:
                    removed_devices = await _fetch_devices(removed_ids, token)
                    removed_exclusive = [
                        uuid.UUID(str(d["id"])) for d in removed_devices if d.get("exclusive", True)
                    ]
                except Exception:
                    removed_exclusive = list(removed_ids)
                if removed_exclusive:
                    await _update_device_statuses(removed_exclusive, "AVAILABLE")

            # Proxy diffs the membership: delete-orphan removes departed devices,
            # new ones are inserted; all flushed in the single commit below.
            reservation.device_ids = list(data.device_ids)

    reservation.modified_by = user_id
    await db.commit()
    await db.refresh(reservation)

    logger.info(
        "Reservation updated: %s",
        reservation.id,
        extra={
            "action": "reservation_update",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
        },
    )

    await _publish_nats_event(
        nats_conn,
        "herd.reservations.updated",
        {
            "event": "reservation.updated",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
            "device_ids": [str(d) for d in reservation.device_ids],
            "added_device_ids": [str(d) for d in added_ids],
            "removed_device_ids": [str(d) for d in removed_ids],
            # Only signal the end time when it actually changed. A metadata-only
            # edit (e.g. purpose) must not advertise an unchanged "ends <time>".
            "end_time_changed": end_time_changed,
            "end_time": reservation.end_time.isoformat() if end_time_changed else None,
        },
    )

    return reservation


async def cancel_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    token: str = "",
    nats_conn=None,
) -> Reservation | None:
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        return None
    if reservation.status in (ReservationStatus.COMPLETED, ReservationStatus.CANCELLED):
        return reservation
    reservation.status = ReservationStatus.CANCELLED
    reservation.modified_by = user_id
    await db.commit()
    await db.refresh(reservation)

    logger.info(
        "Reservation cancelled: %s",
        reservation_id,
        extra={
            "action": "reservation_cancel",
            "reservation_id": str(reservation_id),
            "user_id": str(user_id),
        },
    )

    # Release exclusive devices back to AVAILABLE with bounded retry. The
    # booking is already CANCELLED at this point; if the inventory release
    # fails after retries we log it structured but do NOT revert the cancel
    # (a future reconciliation sweeper handles orphaned RESERVED rows).
    device_ids = list(reservation.device_ids)
    fetch_results = await _fetch_devices_best_effort(device_ids)
    exclusive_ids: list[uuid.UUID] = []
    for did, result in zip(device_ids, fetch_results):
        if isinstance(result, BaseException):
            logger.warning(
                "Could not fetch device %s during cancel; assuming exclusive",
                did,
                exc_info=result,
            )
            exclusive_ids.append(did)
        elif result.get("exclusive", True):
            exclusive_ids.append(did)

    if exclusive_ids:
        try:
            await retry_with_backoff(
                lambda: _update_device_statuses(exclusive_ids, "AVAILABLE", raise_on_failure=True),
                attempts=3,
                initial_delay=0.5,
                factor=2.0,
                max_delay=5.0,
            )
        except Exception as exc:
            logger.error(
                "Reservation cancel: inventory release failed after retries for %s",
                reservation_id,
                extra={
                    "action": "reservation_cancel_release_failed",
                    "reservation_id": str(reservation_id),
                    "user_id": str(user_id),
                    "device_ids": [str(d) for d in exclusive_ids],
                },
                exc_info=exc,
            )

    # Emit NATS event
    await _publish_nats_event(
        nats_conn,
        "herd.reservations.cancelled",
        {
            "event": "reservation.cancelled",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
            "device_ids": [str(d) for d in reservation.device_ids],
            "topology_id": str(reservation.topology_id) if reservation.topology_id else None,
            "topology_type": reservation.topology_type.value,
        },
    )

    return reservation


async def release_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    token: str = "",
    nats_conn=None,
) -> Reservation | None:
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        return None
    if reservation.status != ReservationStatus.ACTIVE:
        return reservation
    reservation.status = ReservationStatus.COMPLETED
    reservation.modified_by = user_id
    await db.commit()
    await db.refresh(reservation)

    logger.info(
        "Reservation released: %s",
        reservation_id,
        extra={
            "action": "reservation_release",
            "reservation_id": str(reservation_id),
            "user_id": str(user_id),
        },
    )

    # Release exclusive devices back to AVAILABLE with bounded retry. Same
    # contract as cancel: reservation stays COMPLETED even if release fails;
    # the failure is structured-logged for a reconciliation sweeper to pick up.
    device_ids = list(reservation.device_ids)
    fetch_results = await _fetch_devices_best_effort(device_ids)
    exclusive_ids: list[uuid.UUID] = []
    for did, result in zip(device_ids, fetch_results):
        if isinstance(result, BaseException):
            logger.warning(
                "Could not fetch device %s during release; assuming exclusive",
                did,
                exc_info=result,
            )
            exclusive_ids.append(did)
        elif result.get("exclusive", True):
            exclusive_ids.append(did)

    if exclusive_ids:
        try:
            await retry_with_backoff(
                lambda: _update_device_statuses(exclusive_ids, "AVAILABLE", raise_on_failure=True),
                attempts=3,
                initial_delay=0.5,
                factor=2.0,
                max_delay=5.0,
            )
        except Exception as exc:
            logger.error(
                "Reservation release: inventory release failed after retries for %s",
                reservation_id,
                extra={
                    "action": "reservation_release_inventory_failed",
                    "reservation_id": str(reservation_id),
                    "user_id": str(user_id),
                    "device_ids": [str(d) for d in exclusive_ids],
                },
                exc_info=exc,
            )

    # Emit NATS event
    await _publish_nats_event(
        nats_conn,
        "herd.reservations.completed",
        {
            "event": "reservation.completed",
            "reservation_id": str(reservation.id),
            "user_id": str(user_id),
            "device_ids": [str(d) for d in reservation.device_ids],
            "topology_id": str(reservation.topology_id) if reservation.topology_id else None,
            "topology_type": reservation.topology_type.value,
        },
    )

    return reservation
