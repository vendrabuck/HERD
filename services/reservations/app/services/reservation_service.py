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
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from herd_common.outbox import enqueue_event
from herd_common.retry import retry_with_backoff
from sqlalchemy import and_, exists, false, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDevice,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.schemas.reservation import ReservationCreate, ReservationUpdate

logger = logging.getLogger(__name__)


async def _fetch_devices(device_ids: list[uuid.UUID], token: str) -> list[dict]:
    """Fetch device info from Inventory service in a single batch call (issue #314).

    Replaces the previous one-GET-per-id asyncio.gather fan-out with inventory's
    POST /devices/batch (issue #250). Preserves the prior all-or-nothing
    semantics exactly: the batch endpoint silently omits ids that are missing or
    not visible to the caller (mirroring the per-id route's 404/403), so a
    request id absent from the response is detected here and raised the same
    way a per-id 404 used to.
    """
    if not device_ids:
        return []

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.inventory_service_url}/devices/batch",
            json={"device_ids": [str(did) for did in device_ids]},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        devices = resp.json()["items"]

    found_ids = {uuid.UUID(str(d["id"])) for d in devices}
    missing = [did for did in device_ids if did not in found_ids]
    if missing:
        raise ValueError(f"Device {missing[0]} not found in inventory")

    return devices


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


async def _fetch_dynamic_templates(template_ids: list[uuid.UUID], token: str) -> list[dict]:
    """Fetch template info from the inventory service concurrently (ADR 0004).

    All-or-nothing, mirroring _fetch_devices: templates referenced by dynamic
    requests are validated at the service boundary before booking, so a missing
    template raises ValueError (422) and a transport failure bubbles up for the
    caller to convert to RuntimeError (503).
    """
    async with httpx.AsyncClient() as client:

        async def fetch_one(template_id: uuid.UUID) -> dict:
            resp = await client.get(
                f"{settings.inventory_service_url}/templates/{template_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            if resp.status_code == 404:
                raise ValueError(f"Template {template_id} not found in inventory")
            resp.raise_for_status()
            return resp.json()

        return await asyncio.gather(*[fetch_one(tid) for tid in template_ids])


async def _validate_dynamic_requests(dynamic_requests, token: str) -> None:
    """Validate every dynamic request's template exists and is a dynamic template.

    Raises ValueError (422) for a missing or wrong-type template and RuntimeError
    (503) when inventory is unreachable, matching the device-validation
    error-code conventions on the create path.
    """
    unique_ids = list(dict.fromkeys(req.template_id for req in dynamic_requests))
    try:
        templates = await _fetch_dynamic_templates(unique_ids, token)
    except ValueError as exc:
        raise exc
    except Exception as exc:
        raise RuntimeError(f"Failed to contact inventory service: {exc}") from exc

    non_dynamic = [str(t["id"]) for t in templates if t.get("template_type") != "dynamic"]
    if non_dynamic:
        raise ValueError(
            f"The following templates are not dynamic templates: {', '.join(non_dynamic)}"
        )


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


async def _create_reservation_fork(
    reservation_id: uuid.UUID,
    topology_id: uuid.UUID | None,
    created_by: str | None = None,
) -> None:
    """Create the editable per-reservation fork in cabling at activation (issue #25).

    Cabling owns the fork (deep-copies the parent canvas, snapshots its relevant
    physical wiring, writes fork_versions v1). We pass parent_version_id=None and
    let cabling pin the parent's current max TopologyVersion itself (Decision 3
    Case B): provisioning already runs at activation against current inventory, so
    the wiring is pinned to the same instant.

    Authenticated as a service-to-service call via X-Internal-Token: the booking
    user does not necessarily own the parent topology.

    Fail-open: a fork-create failure must NOT strand a successfully-provisioned
    reservation. The caller wraps this in retry_with_backoff and, on exhaustion,
    logs and continues, leaving fork_id null. The reservation is still usable; the
    editable bench is created lazily on first edit or by a sweeper (a later PR).
    """
    if topology_id is None:
        # Decision 3 Case A: no parent topology, create the fork lazily on first
        # edit rather than manufacturing an empty fork at activation.
        return
    if not settings.internal_api_token:
        logger.warning(
            "internal_api_token not configured; skipping fork creation for %s",
            reservation_id,
        )
        return

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.cabling_service_url}/internal/forks",
            headers={"X-Internal-Token": settings.internal_api_token},
            json={
                "reservation_id": str(reservation_id),
                "parent_topology_id": str(topology_id),
                "parent_version_id": None,
                "created_by": created_by,
            },
            timeout=10.0,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Cabling fork-create returned {resp.status_code}: {resp.text}")


async def _create_reservation_fork_best_effort(
    reservation_id: uuid.UUID,
    topology_id: uuid.UUID | None,
    created_by: str | None = None,
) -> None:
    """Create the fork with bounded retry and log-and-continue.

    Fork creation is best-effort at activation (issue #25): it must never raise
    out of create_reservation and strand a provisioned reservation. Exhausted
    retries are logged as structured errors and swallowed, leaving the
    reservation ACTIVE with fork_id null. The reservation is still usable on
    the bench; the fork is created lazily on first edit or by a future sweeper.
    This fail-open design ensures provisioning success does not depend on a
    separate cabling service call.
    """
    if topology_id is None:
        return
    try:
        await retry_with_backoff(
            lambda: _create_reservation_fork(reservation_id, topology_id, created_by),
            attempts=3,
            initial_delay=0.5,
            factor=2.0,
            max_delay=5.0,
        )
    except Exception:
        logger.error(
            "Fork creation failed for reservation %s; leaving fork_id null",
            reservation_id,
            extra={
                "action": "reservation_fork_create_failed",
                "reservation_id": str(reservation_id),
                "topology_id": str(topology_id),
            },
            exc_info=True,
        )


async def _check_conflicts(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    start_time: datetime,
    end_time: datetime,
    exclude_id: uuid.UUID | None = None,
    exclusive_device_ids: set[str] | None = None,
) -> list[uuid.UUID]:
    """Return device IDs with conflicting reservations in the given window.

    Conflict is any ACTIVE/PENDING/PENDING_PROVISION reservation overlapping
    [start_time, end_time). PENDING_PROVISION is included (not just ACTIVE)
    so a second create during the provisioning window is rejected; the commit
    of the first reservation's row closes the race against concurrent creates.
    Only exclusive devices are checked when exclusive_device_ids is provided;
    non-exclusive devices may overbuild and so are never conflict-checked.
    Uses indexed join over reservation_devices to avoid N+1 queries.
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


async def _update_device_statuses(
    device_ids: list[uuid.UUID],
    status: str,
    *,
    raise_on_failure: bool = False,
    succeeded: set[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Update device statuses in the inventory service via the internal token.

    Default behavior is best-effort: errors are logged and never raised. Pass
    raise_on_failure=True to raise RuntimeError if any device fails to update;
    callers on the create/cancel/release paths wrap this in retry_with_backoff
    and convert exhausted retries into a structured failure log.

    The per-device POSTs run concurrently, so a partial failure leaves the
    devices that DID succeed already flipped in inventory. Returns the list of
    device ids whose update succeeded. Callers that need to compensate after an
    exhausted retry (where the raised exception discards the return value) may
    pass a mutable ``succeeded`` set; it is updated in place with every device
    that has succeeded on any attempt, so the create path can revert them.
    """
    if not settings.internal_api_token:
        return []

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

    succeeded_ids = [
        did for did, exc in zip(device_ids, results) if not isinstance(exc, BaseException)
    ]
    if succeeded is not None:
        succeeded.update(succeeded_ids)

    failed = [(did, exc) for did, exc in zip(device_ids, results) if isinstance(exc, BaseException)]
    for did, exc in failed:
        logger.error("Failed to update device %s status to %s: %s", did, status, exc, exc_info=exc)

    if failed and raise_on_failure:
        raise RuntimeError(
            f"inventory status update failed for {len(failed)} device(s): "
            f"{[str(d) for d, _ in failed]}"
        )

    return succeeded_ids


async def _acquire_device_locks(db: AsyncSession, device_ids: list[uuid.UUID]) -> None:
    """Acquire PostgreSQL advisory locks for each device before conflict checking.

    Serializes concurrent creates on the same device set to prevent the
    create-window race: both threads fetch AVAILABLE status, both see no
    conflicts (the first reservation row is not yet committed), and both
    commit. Advisory locks are sorted by stringified device_id (stable
    ordering) to prevent deadlocks between concurrent different device sets.
    Locks auto-release on transaction commit. No-op on SQLite (integration
    tests run in-memory without advisory lock support).
    """
    dialect = db.bind.dialect.name if db.bind else ""
    if dialect != "postgresql":
        return
    for device_id_str in sorted(str(d) for d in device_ids):
        lock_key = int(hashlib.sha256(device_id_str.encode()).hexdigest()[:15], 16)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


def _reservation_created_event(reservation: Reservation) -> dict:
    """Build the reservation.created NATS payload from a provisioned reservation.

    Shared by the immediate create path and the scheduled-activation path (issue
    #132) so both emit byte-identical events: execution provisions and
    notifications render an activated scheduled booking exactly like a start-now one.
    """
    return {
        "event": "reservation.created",
        "reservation_id": str(reservation.id),
        "user_id": str(reservation.user_id),
        "device_ids": [str(d) for d in reservation.device_ids],
        "topology_id": str(reservation.topology_id) if reservation.topology_id else None,
        "topology_type": reservation.topology_type.value,
        "start_time": reservation.start_time.isoformat(),
        "end_time": reservation.end_time.isoformat(),
    }


def _provision_requested_event(reservation: Reservation) -> dict:
    """Build the reservation.provision_requested payload (ADR 0004, issue #32).

    Field names are pinned by the execution consumer's _handle_provision_requested:
    it reads reservation_id, user_id, and dynamic_requests entries as
    {"id", "template_id"}, where id is the ledger request_id its create
    idempotency keys on. enqueue_event stamps event_id.
    """
    return {
        "event": "reservation.provision_requested",
        "reservation_id": str(reservation.id),
        "user_id": str(reservation.user_id),
        "device_ids": [str(d) for d in reservation.device_ids],
        "topology_id": str(reservation.topology_id) if reservation.topology_id else None,
        "topology_type": reservation.topology_type.value,
        "dynamic_requests": [
            {"id": str(r.id), "template_id": str(r.template_id)}
            for r in reservation.dynamic_requests
        ],
    }


def _reservation_failed_event(reservation: Reservation) -> dict:
    """Build the reservation.failed payload, shared by every FAILED transition.

    Same shape as the create-path inventory-flip failure so the execution
    teardown consumer and webhooks handle a callback failure or a timeout
    backstop identically.
    """
    return {
        "event": "reservation.failed",
        "reservation_id": str(reservation.id),
        "user_id": str(reservation.user_id),
        "device_ids": [str(d) for d in reservation.device_ids],
        "topology_id": str(reservation.topology_id) if reservation.topology_id else None,
        "topology_type": reservation.topology_type.value,
    }


async def _release_exclusive_devices_best_effort(
    reservation_id: uuid.UUID, device_ids: list[uuid.UUID], log_action: str
) -> None:
    """Release a FAILED reservation's exclusive devices back to AVAILABLE.

    Same discipline as the create path's revert: a FAILED reservation is
    excluded from the conflict status set, so devices left RESERVED would be
    orphaned (unbookable, nothing referencing them). Best-effort with bounded
    retry; the FAILED transition is already committed and is never reverted.
    """
    fetch_results = await _fetch_devices_best_effort(device_ids)
    exclusive_ids: list[uuid.UUID] = []
    for did, result in zip(device_ids, fetch_results):
        if isinstance(result, BaseException) or result.get("exclusive", True):
            exclusive_ids.append(did)
    if not exclusive_ids:
        return
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
            "Inventory release failed after retries for failed reservation %s",
            reservation_id,
            extra={
                "action": log_action,
                "reservation_id": str(reservation_id),
                "device_ids": [str(d) for d in exclusive_ids],
            },
            exc_info=exc,
        )


async def create_reservation(
    db: AsyncSession,
    data: ReservationCreate,
    user_id: uuid.UUID,
    token: str,
    username: str = "",
) -> Reservation:
    # 1. Fetch all devices from inventory (concurrently)
    try:
        devices = await _fetch_devices(data.device_ids, token)
    except ValueError as exc:
        raise exc
    except Exception as exc:
        raise RuntimeError(f"Failed to contact inventory service: {exc}") from exc

    # 2. Derive topology_type. A dynamic-only booking has no physical devices to
    # read it from, so it is CLOUD by construction (ADR 0004 materializes dynamic
    # instances as CLOUD inventory devices); issue #274. With physical devices the
    # existing all-or-nothing uniformity rule stands.
    if devices:
        topology_types = {d["topology_type"] for d in devices}
        if len(topology_types) > 1:
            raise ValueError(
                f"All devices must share the same topology type. Found: {', '.join(topology_types)}"
            )
        topology_type = TopologyType(topology_types.pop())
    else:
        topology_type = TopologyType.CLOUD

    # 2b. If a topology is referenced, validate every edge maps to a real path
    # in the cabling graph. Reservations without a topology are unaffected.
    if data.topology_id is not None:
        await _validate_topology_connectivity(data.topology_id)

    # 2c. Validate dynamic requests against inventory (ADR 0004): every
    # template must exist and be a dynamic template. Reservations without
    # dynamic requests are unaffected.
    has_dynamic = bool(data.dynamic_requests)
    if has_dynamic:
        await _validate_dynamic_requests(data.dynamic_requests, token)

    # Partition devices into exclusive vs non-exclusive
    exclusive_ids = {str(d["id"]) for d in devices if d.get("exclusive", True)}
    non_exclusive_ids = {str(d["id"]) for d in devices if not d.get("exclusive", True)}

    # A booking whose start_time is more than the start-grace ahead is scheduled,
    # not started: it is created PENDING and provisioned by the expiration task at
    # start_time (issue #132), so none of the immediate-provisioning steps below
    # run now. Within the grace (the same "start now" tolerance the request
    # validator allows for past skew) it is treated as immediate, so a start-now
    # click is not made to wait for the next activation tick.
    grace = settings.reservation_start_grace_seconds
    is_future = (data.start_time - datetime.now(timezone.utc)).total_seconds() > grace

    # 3. Check current availability for an immediate ("start now") booking only:
    #    exclusive devices must be AVAILABLE, non-exclusive accept AVAILABLE/RESERVED.
    #    A future booking is gated solely by time-window conflict detection (step 5),
    #    not by what the devices happen to be doing right now, so it can reserve a
    #    device that is busy now but free in the requested window.
    if not is_future:
        bad_exclusive = [
            d["name"]
            for d in devices
            if str(d["id"]) in exclusive_ids and d["status"] != "AVAILABLE"
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
    if is_future:
        # Scheduled: hold the window (the row is visible to _check_conflicts) but
        # defer all provisioning to the activation cycle at start_time.
        initial_status = ReservationStatus.PENDING
    elif exclusive_uuid_ids or has_dynamic:
        # A dynamic-carrying reservation always books through PENDING_PROVISION
        # and activates only on the provision-result callback (ADR 0004), even
        # when it has no exclusive devices to flip.
        initial_status = ReservationStatus.PENDING_PROVISION
    else:
        initial_status = ReservationStatus.ACTIVE
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
    reservation.dynamic_requests = [
        ReservationDynamicRequest(template_id=req.template_id) for req in data.dynamic_requests
    ]
    db.add(reservation)
    # The cascaded reservation_devices rows flush in this same commit, so the
    # PENDING_PROVISION reservation and its device memberships become visible
    # together: exactly what _check_conflicts needs to reject a concurrent create.
    if initial_status == ReservationStatus.ACTIVE:
        # No exclusive devices, so the reservation is ACTIVE immediately with no
        # inventory flip to come. Stage reservation.created in this same
        # transaction (issue #21) so the event commits atomically with the ACTIVE
        # row. flush() first to populate the python-side uuid default used in the
        # payload and to materialize the device memberships.
        await db.flush()
        enqueue_event(
            db,
            OutboxEvent,
            "herd.reservations.created",
            _reservation_created_event(reservation),
        )
    elif initial_status == ReservationStatus.PENDING_PROVISION and not exclusive_uuid_ids:
        # Dynamic requests with no exclusive devices: there is no inventory flip
        # to wait for, so stage provision_requested atomically with the booking.
        # With exclusive devices the event is staged only after the flip below
        # succeeds, so execution never creates instances for a booking that is
        # about to land in FAILED.
        await db.flush()
        # A dynamic-only booking (empty device_ids) never populated the `devices`
        # collection in-memory, so refresh it before the payload builder reads the
        # device_ids association proxy: an unloaded selectin collection would
        # otherwise lazy-load in the sync builder and raise MissingGreenlet (#274).
        # A booking with non-exclusive physical devices already has the collection
        # materialized by the association-proxy creator, so it needs no refresh.
        if not data.device_ids:
            await db.refresh(reservation)
        enqueue_event(
            db,
            OutboxEvent,
            "herd.reservations.provision_requested",
            _provision_requested_event(reservation),
        )
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

    # Scheduled-future booking: stop here. No inventory flip, no fork, no
    # reservation.created. The expiration task's activation cycle runs all of
    # that when start_time arrives (issue #132).
    if is_future:
        return reservation

    # 7. Mark exclusive devices as RESERVED in inventory with retry; non-exclusive
    #    devices stay AVAILABLE so no inventory call is needed. On exhausted retries
    #    the reservation is flipped to FAILED and a reservation.failed event is
    #    staged in the same transaction (issue #33).
    if exclusive_uuid_ids:
        # Track which devices actually reached RESERVED across all retry attempts.
        # The POSTs run concurrently, so a partial failure leaves the succeeding
        # devices flipped in inventory even when the call ultimately raises.
        reserved_ok: set[uuid.UUID] = set()
        try:
            await retry_with_backoff(
                lambda: _update_device_statuses(
                    exclusive_uuid_ids, "RESERVED", raise_on_failure=True, succeeded=reserved_ok
                ),
                attempts=3,
                initial_delay=0.5,
                factor=2.0,
                max_delay=5.0,
            )
        except Exception as exc:
            reservation.status = ReservationStatus.FAILED
            # Stage reservation.failed in the same transaction that lands the row
            # in FAILED (issue #21), so the event exists iff the failure committed.
            # This is the webhook/notification signal that provisioning gave up
            # (issue #33 lists reservation.failed among the delivered events).
            enqueue_event(
                db,
                OutboxEvent,
                "herd.reservations.failed",
                _reservation_failed_event(reservation),
            )
            await db.commit()
            await db.refresh(reservation)
            # Compensate: best-effort revert the devices that DID get set RESERVED
            # back to AVAILABLE. A FAILED reservation is excluded from the conflict
            # status set, so without this the succeeded devices would be orphaned
            # (stuck RESERVED, unbookable) with nothing referencing them. Errors are
            # swallowed and logged; there is no sweeper to clean these up otherwise.
            if reserved_ok:
                try:
                    await _update_device_statuses(sorted(reserved_ok), "AVAILABLE")
                except Exception:
                    logger.error(
                        "Failed to revert RESERVED devices after provisioning failure: %s",
                        reservation.id,
                        extra={
                            "action": "reservation_provision_revert_failed",
                            "reservation_id": str(reservation.id),
                            "device_ids": [str(d) for d in sorted(reserved_ok)],
                        },
                        exc_info=True,
                    )
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

        if has_dynamic:
            # Gated activation (ADR 0004): the exclusive devices are RESERVED,
            # but the reservation stays PENDING_PROVISION until the execution
            # service posts the provision result. Stage provision_requested now
            # that the flip committed cleanly; the callback (or the timeout
            # backstop) owns the next transition.
            enqueue_event(
                db,
                OutboxEvent,
                "herd.reservations.provision_requested",
                _provision_requested_event(reservation),
            )
        else:
            reservation.status = ReservationStatus.ACTIVE
            # Stage reservation.created in the same transaction that lands the row
            # in ACTIVE (issue #21), so the event exists iff provisioning
            # committed. The old fire-and-forget post-commit publish could drop it
            # if NATS was down.
            enqueue_event(
                db,
                OutboxEvent,
                "herd.reservations.created",
                _reservation_created_event(reservation),
            )
        await db.commit()
        await db.refresh(reservation)

    # 8. Create the editable per-reservation fork in cabling now that the
    #    reservation is ACTIVE (issue #25). Best-effort: a fork-create failure must
    #    not strand the provisioned reservation, so this never raises. Skipped when
    #    there is no parent topology (Case A lazy-create). A dynamic-carrying
    #    reservation is still PENDING_PROVISION here; its fork is created by the
    #    provision-result callback when it activates.
    if reservation.status == ReservationStatus.ACTIVE:
        await _create_reservation_fork_best_effort(
            reservation.id, reservation.topology_id, created_by=str(user_id)
        )

    return reservation


async def _claim_provision_transition(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    new_status: ReservationStatus,
) -> bool:
    """Atomically move a reservation out of PENDING_PROVISION (issue #276).

    A conditional UPDATE ... WHERE status = PENDING_PROVISION is a compare-and-
    swap: of the two writers that can leave this state (the provision-result
    callback here and the expiration task's timeout backstop), exactly one finds
    the row still PENDING_PROVISION and performs the transition. Both paths read
    the status and then write it, so without this CAS a success callback landing
    between the backstop's SELECT and its COMMIT could be overwritten to FAILED
    (or the reverse), after which teardown would destroy instances the user
    believes are active. Returns True iff this call performed the transition; a
    False return is the loser and must be a clean no-op (no event, no device
    flip, no teardown).

    synchronize_session is off, so a lost CAS never mutates the caller's
    in-memory object; a winner re-reads via db.refresh where it needs the new
    status. A conditional UPDATE is enforceable on both Postgres and the
    in-memory SQLite used by unit tests, where SELECT ... FOR UPDATE is a silent
    no-op.
    """
    result = await db.execute(
        update(Reservation)
        .where(
            Reservation.id == reservation_id,
            Reservation.status == ReservationStatus.PENDING_PROVISION,
        )
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def apply_provision_result(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    succeeded: bool,
    device_ids: list[str],
    error: str | None = None,
) -> tuple[Reservation | None, bool]:
    """Apply the execution service's provision-result callback (ADR 0004).

    Returns (reservation, applied). None means the reservation does not exist.
    applied=False means the callback was a no-op: only a reservation still in
    PENDING_PROVISION transitions, so a duplicate callback, or one arriving
    after the timeout backstop failed the reservation or the user cancelled it,
    never resurrects or re-transitions the row.

    Success attaches the materialized device ids, activates, and stages the
    existing reservation.created event, so physical L1/L2/L3 provisioning and
    notifications run exactly as today, after the dynamic devices exist; the
    editable fork is created best-effort like every other activation path.
    Failure lands FAILED and stages reservation.failed, whose execution-side
    consumer owns dynamic-instance teardown; the exclusive physical devices are
    released back to AVAILABLE here (the create path's orphaned-RESERVED
    discipline).
    """
    reservation = await db.get(Reservation, reservation_id)
    if reservation is None:
        return None, False
    if reservation.status != ReservationStatus.PENDING_PROVISION:
        # Already terminal: a duplicate or late callback, a user cancel, or the
        # timeout backstop that already moved the row on. No transition, no event.
        return reservation, False

    target = ReservationStatus.ACTIVE if succeeded else ReservationStatus.FAILED
    # Compare-and-swap the transition (issue #276). If the timeout backstop (or a
    # concurrent callback) committed a terminal status between the db.get above
    # and here, this claim matches zero rows and we lose: reload the winner's
    # status and no-op with applied=False, emitting no event and touching no
    # devices, so we never resurrect or double-transition the row.
    if not await _claim_provision_transition(db, reservation_id, target):
        await db.refresh(reservation)
        return reservation, False

    if succeeded:
        existing = {str(d) for d in reservation.device_ids}
        for did in device_ids:
            if did not in existing:
                reservation.device_ids.append(uuid.UUID(did))
                existing.add(did)
        # The CAS already wrote ACTIVE; stage reservation.created in the same
        # transaction so the event commits atomically with the transition.
        enqueue_event(
            db,
            OutboxEvent,
            "herd.reservations.created",
            _reservation_created_event(reservation),
        )
        await db.commit()
        await db.refresh(reservation)
        logger.info(
            "Reservation activated by provision result: %s",
            reservation_id,
            extra={
                "action": "reservation_provision_result_success",
                "reservation_id": str(reservation_id),
            },
        )
        await _create_reservation_fork_best_effort(
            reservation.id, reservation.topology_id, created_by=str(reservation.user_id)
        )
        return reservation, True

    # The CAS already wrote FAILED; stage reservation.failed in the same
    # transaction so the event commits atomically with the transition.
    enqueue_event(
        db,
        OutboxEvent,
        "herd.reservations.failed",
        _reservation_failed_event(reservation),
    )
    await db.commit()
    await db.refresh(reservation)
    logger.error(
        "Reservation failed by provision result: %s: %s",
        reservation_id,
        error,
        extra={
            "action": "reservation_provision_result_failed",
            "reservation_id": str(reservation_id),
            "error": error,
        },
    )
    await _release_exclusive_devices_best_effort(
        reservation.id, list(reservation.device_ids), "reservation_provision_failed_release"
    )
    return reservation, True


async def list_user_reservations(
    db: AsyncSession, user_id: uuid.UUID, skip: int = 0, limit: int = 50
) -> tuple[list[Reservation], int]:
    base = select(Reservation).where(Reservation.user_id == user_id)

    count_query = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        base.order_by(Reservation.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all()), total


async def list_all_reservations(
    db: AsyncSession, skip: int = 0, limit: int = 50
) -> tuple[list[Reservation], int]:
    """Return every reservation across all users, paginated (issue #340).

    Admin-only: the router gates the caller's role before invoking this, so the
    owner filter that list_user_reservations applies is deliberately absent here.
    Ordering mirrors list_user_reservations (newest created first) so the admin
    and self views paginate identically.
    """
    base = select(Reservation)

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
    If visible_device_ids is provided, only include reservations where ALL devices are visible.

    Raises ValueError (422 at the router) when the requested window's span
    exceeds calendar_max_span_days (issue #315): the endpoint has no LIMIT and
    no pagination, so an unbounded client-controlled window would otherwise
    load and hold an unbounded result set in memory. 0 disables the cap.
    """
    max_span_days = settings.calendar_max_span_days
    if max_span_days > 0 and (range_end - range_start) > timedelta(days=max_span_days):
        raise ValueError(
            f"Calendar window cannot exceed {max_span_days} days "
            f"(requested {range_start.isoformat()} to {range_end.isoformat()})"
        )

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
    # Stage reservation.updated in the same transaction as the edit (issue #21).
    # The device membership diff (added/removed) and any inventory flips above are
    # already applied to the session; this commits the event atomically with them.
    enqueue_event(
        db,
        OutboxEvent,
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

    return reservation


async def cancel_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    token: str = "",
    *,
    is_admin: bool = False,
) -> Reservation | None:
    # Owner path first: the owner-filtered load keeps self-cancel byte-for-byte
    # unchanged (including cancelled_by staying NULL). Only when the owner lookup
    # misses AND the caller is an admin do we fall back to an id-only load, which
    # is the admin-cancel-any override (issue #340). A non-admin cancelling a
    # reservation they do not own still misses here and returns None -> 404.
    reservation = await get_reservation(db, reservation_id, user_id)
    admin_override = False
    if reservation is None and is_admin:
        result = await db.execute(select(Reservation).where(Reservation.id == reservation_id))
        reservation = result.scalar_one_or_none()
        admin_override = reservation is not None
    if not reservation:
        return None
    # FAILED is terminal here too: a FAILED reservation already released its
    # devices, and those devices may now be held by a newer ACTIVE reservation.
    # Running the release path would flip that newer reservation's devices to
    # AVAILABLE and emit reservation.cancelled, causing the execution consumer
    # to tear down the newer reservation's live L1/VLAN wiring. No release, no
    # event; FAILED stays FAILED for audit.
    if reservation.status in (
        ReservationStatus.COMPLETED,
        ReservationStatus.CANCELLED,
        ReservationStatus.FAILED,
    ):
        return reservation
    reservation.status = ReservationStatus.CANCELLED
    reservation.modified_by = user_id
    # Record the acting admin only when this is a cross-owner override; an owner
    # self-cancel leaves cancelled_by NULL (issue #340 audit invariant).
    if admin_override:
        reservation.cancelled_by = user_id
    # Stage reservation.cancelled in the same transaction that lands CANCELLED
    # (issue #21). The inventory device release below is best-effort and runs
    # after the commit; the event is durable regardless of NATS availability.
    # The payload's user_id is the reservation OWNER (not the acting caller) so
    # notification fan-out reaches the owner even on an admin override. On the
    # owner path reservation.user_id == user_id, so this is byte-for-byte
    # identical to the prior behavior.
    enqueue_event(
        db,
        OutboxEvent,
        "herd.reservations.cancelled",
        {
            "event": "reservation.cancelled",
            "reservation_id": str(reservation.id),
            "user_id": str(reservation.user_id),
            "device_ids": [str(d) for d in reservation.device_ids],
            "topology_id": str(reservation.topology_id) if reservation.topology_id else None,
            "topology_type": reservation.topology_type.value,
        },
    )
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

    return reservation


async def release_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    user_id: uuid.UUID,
    token: str = "",
) -> Reservation | None:
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        return None
    if reservation.status != ReservationStatus.ACTIVE:
        return reservation
    reservation.status = ReservationStatus.COMPLETED
    reservation.modified_by = user_id
    # Stage reservation.completed in the same transaction that lands COMPLETED
    # (issue #21), mirroring the auto-expiry path. Inventory device release below
    # is best-effort; the event is durable regardless of NATS availability.
    enqueue_event(
        db,
        OutboxEvent,
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

    return reservation
