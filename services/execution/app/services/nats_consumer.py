"""NATS consumer: subscribe to reservation lifecycle events and trigger driver execution."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

import httpx
from herd_common.outbox import event_dedupe_key

from app.config import settings

logger = logging.getLogger(__name__)


# Map NATS event types to driver actions. reservation.failed is best-effort
# teardown of whatever provisioning had already landed when the reservation
# gave up (issue #244); handle_reservation_event drives it applied-state-only,
# so a reservation that failed before any driver action ran tears down nothing.
EVENT_ACTIONS = {
    "reservation.created": "connect_ports",
    "reservation.cancelled": "disconnect_ports",
    "reservation.completed": "disconnect_ports",
    "reservation.updated": "update_ports",
    "reservation.failed": "disconnect_ports",
}

# JetStream consumer policy. Redelivery backoff tracks max_deliver length.
NATS_MAX_DELIVER = 5
NATS_ACK_WAIT_SECONDS = 30
NATS_BACKOFF_SECONDS = [1, 5, 15, 60, 120]
# Pull-consumer fetch tuning. A pull consumer re-establishes on the next fetch
# after a broker reconnect, which a push subscription does not do reliably, so it
# survives a NATS restart (issue #21).
NATS_FETCH_BATCH = 10
NATS_FETCH_TIMEOUT_SECONDS = 5
# DLQ subject is 4 tokens so the consumer's 3-token "herd.reservations.*" filter
# (single-token wildcard matches exactly one token) does NOT match it. If it did,
# every DLQ'd message would be redelivered to this same consumer: a poison message
# would loop forever and a max_deliver-exhausted message would re-run the
# non-idempotent handler. This mirrors the notifications service's
# "herd.reservations.dlq.notifications". This 4-token subject is not bound to the
# HERD_RESERVATIONS source stream (subjects=["herd.reservations.*"]); instead it is
# captured by the dedicated HERD_DLQ stream (subjects=["herd.*.dlq.>"]), created at
# execution startup by _ensure_dlq_stream, which retains DLQ'd messages for
# inspection and replay (see docs/OPERATIONS.md). The _publish_to_dlq publish stays
# best-effort and swallows errors so a DLQ outage cannot wedge the consumer.
NATS_DLQ_SUBJECT = "herd.reservations.dlq.execution"


class TransientUpstreamError(RuntimeError):
    """An inventory/cabling call failed in a way that may succeed on retry.

    Raised on a 5xx response or a transport error (connect failure, timeout).
    Propagating it out of the event handler lets process_reservation_message
    NAK the message so JetStream retries with backoff, instead of the fetch
    helper swallowing the failure as "not found" and silently half-provisioning.
    A genuine 404 is NOT transient: helpers still return None/[] for that.
    """


class PermanentEventError(RuntimeError):
    """A reservation event failed in a way that retry cannot fix.

    Sibling of TransientUpstreamError. Raised for non-retryable conditions
    (e.g. a fabric's VLAN pool is exhausted: the in-use set is unchanged
    between delivery attempts, so backoff-and-retry only wastes max_deliver
    attempts before DLQ'ing). process_reservation_message routes this straight
    to the DLQ on the FIRST delivery, with a distinct log phrase, rather than
    NAK'ing through the full backoff schedule like a transient failure.
    """


async def _get_internal(client, url, *, what, **kwargs):
    """GET an internal service URL, raising TransientUpstreamError on a failure
    that retry might fix (5xx or transport error). Returns the httpx.Response
    so the caller can handle 404 / other non-200 statuses itself.
    """
    try:
        resp = await client.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise TransientUpstreamError(f"{what}: transport error: {exc}") from exc
    if resp.status_code >= 500:
        raise TransientUpstreamError(f"{what}: upstream {resp.status_code}")
    return resp


class _AsyncNullCtx:
    """Async context manager that yields a pre-existing object without closing it.

    Lets fetch helpers write `async with _client_ctx(client) as c:` uniformly
    whether they own a freshly-opened client (which must be closed) or are
    reusing a per-event client owned by the caller (which must NOT be closed
    here). This abstraction enables per-event client pooling (issue #137):
    one httpx.AsyncClient for the whole event, reused across many fetches,
    versus the prior per-call behavior (open, use, close every fetch).
    """

    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *args):
        return False


def _client_ctx(client):
    """Return an async context manager yielding an httpx client.

    When `client` is None the helper opens (and closes) its own AsyncClient,
    preserving the standalone per-call behavior. When a client is supplied the
    caller owns its lifecycle, so we yield it without closing: this is how a
    single per-event client gets reused across many fetches (issue #137).
    """
    if client is None:
        return httpx.AsyncClient()
    return _AsyncNullCtx(client)


async def _fetch_connections_for_device(device_id: str, client=None) -> list[dict]:
    """Fetch connections involving a device from the cabling service.

    Raises TransientUpstreamError on a 5xx or transport error so the message
    NAKs and retries; a non-200 below 500 returns [] (treated as no
    connections), matching the prior behavior for the 404 case.

    When `client` is provided it is reused (per-event connection pooling, issue
    #137); when None a fresh client is opened for this one call as before.
    """
    url = f"{settings.cabling_service_url}/connections/internal"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch connections for device {device_id}",
            params={"device_id": device_id, "limit": 500},
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning(
                "Failed to fetch connections for device %s: %s", device_id, resp.status_code
            )
            return []
        data = resp.json()
        return data.get("items", data) if isinstance(data, dict) else data


async def _fetch_device(device_id: str, client=None) -> dict | None:
    """Fetch device details from inventory service.

    Raises TransientUpstreamError on a 5xx or transport error so the message
    NAKs and retries; a non-200 below 500 (e.g. a genuine 404) returns None.

    When `client` is provided it is reused (per-event connection pooling, issue
    #137); when None a fresh client is opened for this one call as before.
    """
    url = f"{settings.inventory_service_url}/devices/{device_id}/internal"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch device {device_id}",
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json()


async def _fetch_template(template_id: str, client=None) -> dict | None:
    """Fetch template details from inventory service.

    Raises TransientUpstreamError on a 5xx or transport error so the message
    NAKs and retries; a non-200 below 500 (e.g. a genuine 404) returns None.

    When `client` is provided it is reused (per-event connection pooling, issue
    #137); when None a fresh client is opened for this one call as before.
    """
    url = f"{settings.inventory_service_url}/templates/{template_id}/internal"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch template {template_id}",
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json()


async def _fetch_latest_config(device_id: str, client=None) -> dict | None:
    """Fetch a device's latest config version from the inventory service.

    Feeds L3 route provisioning (issue #20): the consumer has no acting user,
    so it reads the X-Internal-Token endpoint added for this purpose. Raises
    TransientUpstreamError on a 5xx or transport error so the message NAKs and
    retries; a non-200 below 500 (no versions, unknown device) returns None,
    which the caller treats as "no routes to provision".

    When `client` is provided it is reused (per-event connection pooling, issue
    #137); when None a fresh client is opened for this one call as before.
    """
    url = f"{settings.inventory_service_url}/devices/{device_id}/config-versions/latest/internal"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch latest config for device {device_id}",
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json()


class _FetchContext:
    """Per-event fetch context: one shared httpx client plus memoization caches.

    Created once per reservation lifecycle event (issue #137) so the L1, L2,
    and L3 resolver passes (and the later execution phase) reuse a single
    connection pool and never re-fetch the same device's connections or the
    same far-end device twice. This is a performance critical optimization:
    dense topologies can have many connections, and without memoization each
    connection's far-end device would be fetched N times. The caches map:
      - conn_cache:   device_id -> list[connection dict]
      - device_cache: device_id -> device dict | None
      - config_cache: device_id -> latest config version dict | None

    A None entry in device_cache or config_cache is a real cached "not found"
    (404), so a missing far end or config is classified at most once per
    event. The client is owned by the caller (handle_reservation_event); these
    helpers never close it.
    """

    def __init__(self, client):
        self._client = client
        self._conn_cache: dict[str, list[dict]] = {}
        self._device_cache: dict[str, dict | None] = {}
        self._config_cache: dict[str, dict | None] = {}

    @property
    def client(self):
        return self._client

    async def get_connections(self, device_id: str) -> list[dict]:
        """Return a device's connections, fetching at most once per event."""
        if device_id not in self._conn_cache:
            self._conn_cache[device_id] = await _fetch_connections_for_device(
                device_id, self._client
            )
        return self._conn_cache[device_id]

    async def get_device(self, device_id: str) -> dict | None:
        """Return a far-end device, classifying it at most once per event."""
        if device_id not in self._device_cache:
            self._device_cache[device_id] = await _fetch_device(device_id, self._client)
        return self._device_cache[device_id]

    async def get_latest_config(self, device_id: str) -> dict | None:
        """Return a device's latest config version, fetching at most once per event."""
        if device_id not in self._config_cache:
            self._config_cache[device_id] = await _fetch_latest_config(device_id, self._client)
        return self._config_cache[device_id]


async def _resolve_l1_switch_operations(
    device_ids: list[str],
    ctx: "_FetchContext | None" = None,
) -> list[dict]:
    """Resolve which L1 switch operations are needed for a set of reserved devices.

    Returns a list of dicts with keys:
      switch_device_id, switch_port_a, switch_port_b
    representing port pairs on L1 switches that need to be connected/disconnected.

    `ctx` carries the per-event shared client and memoization caches (issue
    #137). When None (e.g. a direct unit-test call) a throwaway context with no
    shared client is used, so connections/devices are fetched per call as before.
    """
    if ctx is None:
        ctx = _FetchContext(None)
    operations = []

    # For each reserved device, find connections where the other side is an L1 switch
    for device_id in device_ids:
        connections = await ctx.get_connections(device_id)
        for conn in connections:
            # Determine which side is the reserved device and which might be the L1 switch
            if str(conn.get("device_a_id")) == device_id:
                other_device_id = str(conn.get("device_b_id"))
            elif str(conn.get("device_b_id")) == device_id:
                other_device_id = str(conn.get("device_a_id"))
            else:
                continue

            # Check if the other device is an L1 switch
            other_device = await ctx.get_device(other_device_id)
            if not other_device:
                continue
            if other_device.get("connection_type") != "Layer 1 Switch":
                continue

            # This connection goes through an L1 switch
            # The switch ports are the ports on the switch side
            if str(conn.get("device_a_id")) == other_device_id:
                switch_port = conn.get("port_a")
            else:
                switch_port = conn.get("port_b")

            operations.append(
                {
                    "switch_device_id": other_device_id,
                    "switch_port": switch_port,
                    "dut_device_id": device_id,
                }
            )

    # Group operations by switch and pair them up
    # An L1 switch connects two DUT ports through its own ports
    switch_ops = {}
    for op in operations:
        sid = op["switch_device_id"]
        if sid not in switch_ops:
            switch_ops[sid] = []
        switch_ops[sid].append(op["switch_port"])

    # Create port pair operations (connect consecutive pairs)
    paired = []
    for switch_id, ports in switch_ops.items():
        if len(ports) % 2 != 0:
            logger.warning(
                "Odd number of ports (%d) for switch %s; last port will not be paired",
                len(ports),
                switch_id,
            )
        # Pair ports sequentially (port 0 to port 1, port 2 to port 3, etc.)
        for i in range(0, len(ports) - 1, 2):
            paired.append(
                {
                    "switch_device_id": switch_id,
                    "switch_port_a": ports[i],
                    "switch_port_b": ports[i + 1],
                }
            )

    return paired


def _derive_vlan_id(reservation_id: str) -> int:
    """Derive a deterministic VLAN ID (2-4094) from a reservation UUID."""
    return (uuid.UUID(reservation_id).int % 4093) + 2


async def _resolve_l2_switch_operations(
    device_ids: list[str],
    ctx: "_FetchContext | None" = None,
) -> list[dict]:
    """Resolve which L2 switch operations are needed for a set of reserved devices.

    Returns a list of dicts with keys:
      switch_device_id, switch_port, tag
    representing per-port VLAN operations on L2 switches.
    VLAN ID is NOT set here; it is assigned per-fabric by the caller.
    Unlike L1, L2 operations are per-port (not paired).

    `ctx` carries the per-event shared client and memoization caches (issue
    #137). When None (e.g. a direct unit-test call) a throwaway context with no
    shared client is used, so connections/devices are fetched per call as before.
    """
    if ctx is None:
        ctx = _FetchContext(None)
    operations = []

    for device_id in device_ids:
        connections = await ctx.get_connections(device_id)
        for conn in connections:
            if str(conn.get("device_a_id")) == device_id:
                other_device_id = str(conn.get("device_b_id"))
            elif str(conn.get("device_b_id")) == device_id:
                other_device_id = str(conn.get("device_a_id"))
            else:
                continue

            other_device = await ctx.get_device(other_device_id)
            if not other_device:
                continue
            if other_device.get("connection_type") != "Layer 2 Switch":
                continue

            # Get the switch-side port
            if str(conn.get("device_a_id")) == other_device_id:
                switch_port = conn.get("port_a")
            else:
                switch_port = conn.get("port_b")

            operations.append(
                {
                    "switch_device_id": other_device_id,
                    "switch_port": switch_port,
                    "tag": "tagged",
                }
            )

    return operations


async def _assign_vlans_to_operations(
    operations: list[dict],
    reservation_id: str,
    get_db_session,
) -> list[dict]:
    """Assign fabric-aware VLAN IDs to L2 switch operations.

    Groups switches by fabric, assigns a conflict-free VLAN per fabric,
    and sets vlan_id on each operation dict.
    """
    from app.services.vlan_service import fetch_fabric_id, find_or_assign_vlan

    # Fetch fabric ID for each unique switch
    switch_ids = {op["switch_device_id"] for op in operations}
    switch_fabric: dict[str, uuid.UUID] = {}
    for sid in switch_ids:
        fid = await fetch_fabric_id(sid)
        if fid is None:
            # Fallback: treat switch as its own isolated fabric
            fid = uuid.uuid5(uuid.NAMESPACE_DNS, sid)
            logger.warning("Could not determine fabric for switch %s, using fallback", sid)
        switch_fabric[sid] = fid

    # Group switch IDs by fabric
    fabric_switches: dict[uuid.UUID, list[str]] = {}
    for sid, fid in switch_fabric.items():
        fabric_switches.setdefault(fid, []).append(sid)

    # Assign a VLAN per fabric
    fabric_vlan: dict[uuid.UUID, int] = {}
    async with get_db_session() as db:
        for fid, sids in fabric_switches.items():
            vlan_id = await find_or_assign_vlan(db, reservation_id, fid, sids)
            fabric_vlan[fid] = vlan_id

    # Set vlan_id on each operation
    for op in operations:
        fid = switch_fabric[op["switch_device_id"]]
        op["vlan_id"] = fabric_vlan[fid]

    return operations


async def _execute_l2_switch_operations(
    device_ids: list[str],
    l2_action: str,
    reservation_id: str | None,
    user_id: str,
    get_db_session,
    dedupe_key: str | None = None,
    ctx: "_FetchContext | None" = None,
    failed_cleanup: bool = False,
) -> None:
    """Resolve L2 switch operations for devices and execute VLAN provisioning.

    Args:
        device_ids: devices to resolve switch operations for
        l2_action: "provision" (create VLAN + add ports) or
            "deprovision" (remove ports + delete VLAN)
        reservation_id: reservation UUID string
        user_id: user UUID string
        get_db_session: async context manager that yields an AsyncSession
        dedupe_key: source-message key; a VLAN action whose SUCCESS run already
            carries it is skipped on redelivery (issue #133).
        ctx: per-event fetch context (shared client + caches, issue #137). When
            None a throwaway context is created so a direct call still works.
        failed_cleanup: reservation.failed teardown (issue #244), deprovision
            only. Drives teardown strictly from the stored ACTIVE
            vlan_assignments: a switch with no stored assignment never got
            create_vlan, so there is no derived-VLAN fallback and no driver
            call for it. Rows are read first and RELEASED only after the
            switches were driven (the L3 release-after ordering), so a
            transient NAK mid-teardown redelivers with the rows still ACTIVE.
    """
    if not reservation_id:
        logger.warning("No reservation_id for L2 operations, skipping")
        return

    if ctx is None:
        ctx = _FetchContext(None)

    operations = await _resolve_l2_switch_operations(device_ids, ctx)

    if l2_action == "deprovision" and failed_cleanup:
        # reservation.failed (issue #244): tear down exactly what the stored
        # assignments say was assigned, and nothing else. The cancel/complete
        # branch below releases the rows up front because it can fall back to a
        # derived VLAN on redelivery; this path has no fallback by design, so
        # it reads the rows here and releases them at the end instead.
        from app.services.vlan_service import get_vlan_assignments

        async with get_db_session() as db:
            assignments = await get_vlan_assignments(db, reservation_id)

        if not assignments:
            logger.info(
                "No ACTIVE VLAN assignments for FAILED reservation %s; no L2 state to tear down",
                reservation_id,
            )
            return

        switch_vlan = {}
        for a in assignments:
            for sid in a.switch_device_ids:
                switch_vlan[sid] = a.vlan_id
        operations = [op for op in operations if op["switch_device_id"] in switch_vlan]
        for op in operations:
            op["vlan_id"] = switch_vlan[op["switch_device_id"]]
    elif l2_action == "deprovision":
        # For deprovisioning, look up stored VLAN assignments first.
        # If assignments exist, use the stored VLAN IDs.
        # If not (legacy reservation), fall back to derived VLAN ID.
        from app.services.vlan_service import release_vlan

        async with get_db_session() as db:
            assignments = await release_vlan(db, reservation_id)

        if assignments:
            # Build a map of switch_id to vlan_id from stored assignments
            switch_vlan: dict[str, int] = {}
            for a in assignments:
                for sid in a.switch_device_ids:
                    switch_vlan[sid] = a.vlan_id
            for op in operations:
                sid = op["switch_device_id"]
                op["vlan_id"] = switch_vlan.get(sid, _derive_vlan_id(reservation_id))
        else:
            # Legacy: no stored assignment, fall back to derived VLAN
            legacy_vlan = _derive_vlan_id(reservation_id)
            for op in operations:
                op["vlan_id"] = legacy_vlan
    else:
        # For provisioning, assign fabric-aware VLANs
        if not operations:
            logger.info("No L2 switch operations needed for reservation %s", reservation_id)
            return
        operations = await _assign_vlans_to_operations(operations, reservation_id, get_db_session)

    if not operations:
        logger.info("No L2 switch operations needed for reservation %s", reservation_id)
        return

    from datetime import datetime, timezone

    from app.services.driver_loader import load_driver
    from app.services.driver_sandbox import execute_driver_method
    from app.services.execution_service import (
        action_already_succeeded,
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )

    # Group by switch
    switch_groups: dict[str, list[dict]] = {}
    for op in operations:
        sid = op["switch_device_id"]
        if sid not in switch_groups:
            switch_groups[sid] = []
        switch_groups[sid].append(op)

    for switch_id, ops in switch_groups.items():
        switch_data = await ctx.get_device(switch_id)
        if not switch_data:
            logger.error("L2 switch %s not found", switch_id)
            continue

        template_data = await _fetch_template(switch_data.get("template_id", ""), ctx.client)
        if not template_data:
            logger.error("Template for L2 switch %s not found", switch_id)
            continue

        switch_uuid = uuid.UUID(switch_id)
        user_uuid = uuid.UUID(user_id)
        res_uuid = uuid.UUID(reservation_id)

        context = build_context(switch_data, switch_uuid, user_uuid, res_uuid)
        password_keys = extract_password_keys(template_data)
        redacted = redact_context_for_logging(context, password_keys)

        driver_id = uuid.UUID(switch_data["driver_id"])
        driver_sha256 = switch_data.get("driver_sha256", "unknown")
        driver_filename = switch_data.get("driver_filename", "driver.zip")
        connection_type = switch_data.get("connection_type", "Layer 2 Switch")

        vlan_id = ops[0]["vlan_id"]

        async with get_db_session() as db:
            try:
                driver_path = await load_driver(
                    db, driver_id, driver_sha256, driver_filename, connection_type
                )
            except Exception as e:
                logger.error("Failed to load driver for L2 switch %s: %s", switch_id, e)
                continue

            # Login
            login_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "login",
                user_uuid,
                redacted,
                res_uuid,
                dedupe_key=dedupe_key,
            )
            started = datetime.now(timezone.utc)
            login_result = execute_driver_method(
                driver_path, "login", context, password_keys=password_keys
            )
            if login_result["success"]:
                await update_execution_run(
                    db,
                    login_run,
                    "SUCCESS",
                    output=json.dumps(login_result["output"], default=str),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=login_result["duration_ms"],
                )
            else:
                await update_execution_run(
                    db,
                    login_run,
                    "FAILED",
                    error=login_result.get("error"),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=login_result["duration_ms"],
                )
                logger.error("Login failed for L2 switch %s, skipping VLAN operations", switch_id)
                continue

            if l2_action == "provision":
                # Create VLAN first (skip if this message already created it)
                vlan_kwargs = {"vlan_id": vlan_id}
                if await action_already_succeeded(
                    db, dedupe_key, switch_uuid, "create_vlan", None, None
                ):
                    logger.info(
                        "Skipping already-applied create_vlan on switch %s; idempotent replay",
                        switch_id,
                    )
                else:
                    run = await create_execution_run(
                        db,
                        switch_uuid,
                        driver_id,
                        driver_sha256,
                        "create_vlan",
                        user_uuid,
                        redacted,
                        res_uuid,
                        method_kwargs=vlan_kwargs,
                        dedupe_key=dedupe_key,
                    )
                    op_started = datetime.now(timezone.utc)
                    result = execute_driver_method(
                        driver_path,
                        "create_vlan",
                        context,
                        method_kwargs=vlan_kwargs,
                        password_keys=password_keys,
                    )
                    status = "SUCCESS" if result["success"] else "FAILED"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )

                # Add each port to the VLAN
                for op in ops:
                    port = op["switch_port"]
                    if await action_already_succeeded(
                        db, dedupe_key, switch_uuid, "add_to_vlan", port, None
                    ):
                        logger.info(
                            "Skipping already-applied add_to_vlan on switch %s port %s; "
                            "idempotent replay",
                            switch_id,
                            port,
                        )
                        continue
                    port_kwargs = {"port": port, "vlan_id": vlan_id, "tag": op["tag"]}
                    run = await create_execution_run(
                        db,
                        switch_uuid,
                        driver_id,
                        driver_sha256,
                        "add_to_vlan",
                        user_uuid,
                        redacted,
                        res_uuid,
                        port,
                        method_kwargs=port_kwargs,
                        dedupe_key=dedupe_key,
                    )
                    op_started = datetime.now(timezone.utc)
                    result = execute_driver_method(
                        driver_path,
                        "add_to_vlan",
                        context,
                        method_kwargs=port_kwargs,
                        password_keys=password_keys,
                    )
                    status = "SUCCESS" if result["success"] else "FAILED"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )

            elif l2_action == "deprovision":
                # Remove each port from the VLAN first
                for op in ops:
                    port = op["switch_port"]
                    if await action_already_succeeded(
                        db, dedupe_key, switch_uuid, "remove_from_vlan", port, None
                    ):
                        logger.info(
                            "Skipping already-applied remove_from_vlan on switch %s port %s; "
                            "idempotent replay",
                            switch_id,
                            port,
                        )
                        continue
                    port_kwargs = {"port": port, "vlan_id": vlan_id}
                    run = await create_execution_run(
                        db,
                        switch_uuid,
                        driver_id,
                        driver_sha256,
                        "remove_from_vlan",
                        user_uuid,
                        redacted,
                        res_uuid,
                        port,
                        method_kwargs=port_kwargs,
                        dedupe_key=dedupe_key,
                    )
                    op_started = datetime.now(timezone.utc)
                    result = execute_driver_method(
                        driver_path,
                        "remove_from_vlan",
                        context,
                        method_kwargs=port_kwargs,
                        password_keys=password_keys,
                    )
                    status = "SUCCESS" if result["success"] else "FAILED"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )

                # Delete the VLAN (skip if this message already deleted it)
                if await action_already_succeeded(
                    db, dedupe_key, switch_uuid, "delete_vlan", None, None
                ):
                    logger.info(
                        "Skipping already-applied delete_vlan on switch %s; idempotent replay",
                        switch_id,
                    )
                else:
                    vlan_kwargs = {"vlan_id": vlan_id}
                    run = await create_execution_run(
                        db,
                        switch_uuid,
                        driver_id,
                        driver_sha256,
                        "delete_vlan",
                        user_uuid,
                        redacted,
                        res_uuid,
                        method_kwargs=vlan_kwargs,
                        dedupe_key=dedupe_key,
                    )
                    op_started = datetime.now(timezone.utc)
                    result = execute_driver_method(
                        driver_path,
                        "delete_vlan",
                        context,
                        method_kwargs=vlan_kwargs,
                        password_keys=password_keys,
                    )
                    status = "SUCCESS" if result["success"] else "FAILED"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )

            # Logout
            logout_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "logout",
                user_uuid,
                redacted,
                res_uuid,
                dedupe_key=dedupe_key,
            )
            logout_started = datetime.now(timezone.utc)
            logout_result = execute_driver_method(
                driver_path, "logout", context, password_keys=password_keys
            )
            status = "SUCCESS" if logout_result["success"] else "FAILED"
            await update_execution_run(
                db,
                logout_run,
                status,
                output=(
                    json.dumps(logout_result["output"], default=str)
                    if logout_result.get("output")
                    else None
                ),
                error=logout_result.get("error"),
                started_at=logout_started,
                completed_at=datetime.now(timezone.utc),
                duration_ms=logout_result["duration_ms"],
            )

    # reservation.failed teardown releases the rows only now, after the
    # switches were driven (see the failed_cleanup docstring). A transient
    # upstream error has already raised and NAKed above, keeping the rows
    # ACTIVE for the redelivery; driver-result failures do not raise and the
    # teardown is best-effort, matching the cancel path's posture.
    if l2_action == "deprovision" and failed_cleanup:
        from app.services.vlan_service import release_vlan

        async with get_db_session() as db:
            await release_vlan(db, reservation_id)


# Map NATS events to L2 actions. reservation.failed tears down only what the
# stored vlan_assignments record as assigned (issue #244); see failed_cleanup
# in _execute_l2_switch_operations.
L2_EVENT_ACTIONS = {
    "reservation.created": "provision",
    "reservation.cancelled": "deprovision",
    "reservation.completed": "deprovision",
    "reservation.failed": "deprovision",
}


async def _resolve_l3_switch_operations(
    device_ids: list[str],
    ctx: "_FetchContext | None" = None,
) -> list[str]:
    """Resolve which L3 switches serve a set of reserved devices.

    Returns a deduplicated list of L3 switch device ids: a switch participates
    in a reservation iff a cabling connection links a reserved device to it,
    the same adjacency rule L1 and L2 use. Unlike L1/L2 no port names are
    collected; the routes an L3 switch applies come from its own latest config
    version, not from the topology edge (issue #20).

    `ctx` carries the per-event shared client and memoization caches (issue
    #137). When None (e.g. a direct unit-test call) a throwaway context with no
    shared client is used, so connections/devices are fetched per call as before.
    """
    if ctx is None:
        ctx = _FetchContext(None)
    switches: list[str] = []
    seen: set[str] = set()

    for device_id in device_ids:
        connections = await ctx.get_connections(device_id)
        for conn in connections:
            if str(conn.get("device_a_id")) == device_id:
                other_device_id = str(conn.get("device_b_id"))
            elif str(conn.get("device_b_id")) == device_id:
                other_device_id = str(conn.get("device_a_id"))
            else:
                continue

            if other_device_id in seen:
                continue
            other_device = await ctx.get_device(other_device_id)
            if not other_device:
                continue
            if other_device.get("connection_type") != "Layer 3 Switch":
                continue

            seen.add(other_device_id)
            switches.append(other_device_id)

    return switches


def _route_run_identity(destination, next_hop, interface) -> tuple[str, str]:
    """Identity of one route within a switch+action, for the idempotency guard.

    ExecutionRun exposes two free-form columns (port_a/port_b), but a route is
    uniquely identified by three fields: (destination, next_hop, interface). Two
    routes to the same prefix out the same interface via different next hops are
    distinct ECMP paths and must NOT collapse into one guarded action. So
    destination goes in port_a, and next_hop plus interface are packed into
    port_b, letting all three fields participate in action_already_succeeded.
    next_hop may be None (an interface route); it renders as empty.
    """
    return destination, f"{interface}|{next_hop or ''}"


async def _execute_l3_switch_operations(
    device_ids: list[str],
    l3_action: str,
    reservation_id: str | None,
    user_id: str,
    get_db_session,
    dedupe_key: str | None = None,
    ctx: "_FetchContext | None" = None,
    remaining_device_ids: list[str] | None = None,
) -> None:
    """Resolve L3 switches for devices and execute route provisioning.

    Provision applies exactly the routes the switch's latest inventory config
    version declares at the moment of provisioning, pinned in
    route_assignments; deprovision removes exactly the pinned set and never
    re-derives from the config (issue #20).

    State ordering differs from the L2 executor on purpose. L2 flips its VLAN
    assignment to RELEASED before driving the switch because it can fall back
    to a derived VLAN on redelivery. L3 has no such fallback, so deprovision
    reads the ACTIVE assignments first, drives the switch, and releases each
    switch's pin only AFTER that switch's routes were all removed cleanly. A
    TransientUpstreamError propagates and NAKs (the rows stay ACTIVE for the
    redelivery, whose per-route dedupe guards skip the removals that already
    succeeded). A driver-result failure (remove_route returns success=False,
    e.g. the switch is unreachable) does NOT raise and the message is ACKed, so
    that switch's pin is deliberately kept ACTIVE rather than released: an
    ACTIVE pin means "routes may still be installed", which stays accurate for
    the stranded-teardown cleanup path (issue #244). Releasing it would falsely
    record removal.

    Args:
        device_ids: devices to resolve switch adjacency for
        l3_action: "provision" (configure_route per pinned route) or
            "deprovision" (remove_route per pinned route)
        reservation_id: reservation UUID string
        user_id: user UUID string
        get_db_session: async context manager that yields an AsyncSession
        dedupe_key: source-message key; a route action whose SUCCESS run
            already carries it is skipped on redelivery (issue #133).
        ctx: per-event fetch context (shared client + caches, issue #137).
        remaining_device_ids: only for the reservation.updated removal path;
            the post-edit device set. A switch is deprovisioned only when it is
            no longer adjacent to any of these devices, so a switch still
            serving the reservation keeps its routes.
    """
    if not reservation_id:
        logger.warning("No reservation_id for L3 operations, skipping")
        return

    if ctx is None:
        ctx = _FetchContext(None)

    from app.services.route_service import (
        assign_routes,
        get_pinned_routes,
        get_route_assignments,
        release_routes_for_device,
    )

    # Build the per-switch route work. For provision the routes come from the
    # pinned assignment (created here on first delivery); for deprovision they
    # come exclusively from the stored ACTIVE assignments.
    switch_routes: dict[str, list[dict]] = {}

    if l3_action == "provision":
        switch_ids = await _resolve_l3_switch_operations(device_ids, ctx)
        if not switch_ids:
            logger.info("No L3 switch operations needed for reservation %s", reservation_id)
            return
        async with get_db_session() as db:
            for sid in switch_ids:
                pinned = await get_pinned_routes(db, reservation_id, sid)
                if pinned is None:
                    detail = await ctx.get_latest_config(sid)
                    fetched = ((detail or {}).get("config") or {}).get("routes") or []
                    if not fetched:
                        logger.info(
                            "L3 switch %s has no routes in its latest config version; "
                            "skipping for reservation %s",
                            sid,
                            reservation_id,
                        )
                        continue
                    pinned = await assign_routes(db, reservation_id, sid, fetched)
                if pinned:
                    switch_routes[sid] = pinned
    else:
        async with get_db_session() as db:
            assignments = await get_route_assignments(db, reservation_id)
        if remaining_device_ids is not None:
            # reservation.updated removal: deprovision only the switches that no
            # longer serve any remaining reserved device.
            removed_switches = set(await _resolve_l3_switch_operations(device_ids, ctx))
            still_serving = set(await _resolve_l3_switch_operations(remaining_device_ids, ctx))
            departing = removed_switches - still_serving
            assignments = [a for a in assignments if str(a.device_id) in departing]
        for a in assignments:
            switch_routes[str(a.device_id)] = a.routes

    if not switch_routes:
        logger.info("No L3 switch operations needed for reservation %s", reservation_id)
        return

    from datetime import datetime, timezone

    from app.services.driver_loader import load_driver
    from app.services.driver_sandbox import execute_driver_method
    from app.services.execution_service import (
        action_already_succeeded,
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )

    method = "configure_route" if l3_action == "provision" else "remove_route"

    # Deprovision releases a switch's pin only after all its routes were removed
    # cleanly; a switch that failed to load, failed login, or had any FAILED
    # remove_route keeps its ACTIVE pin (see docstring). Skipped-already-removed
    # routes on redelivery count as clean.
    cleanly_removed: set[str] = set()

    for switch_id, routes in switch_routes.items():
        switch_data = await ctx.get_device(switch_id)
        if not switch_data:
            logger.error("L3 switch %s not found", switch_id)
            continue

        template_data = await _fetch_template(switch_data.get("template_id", ""), ctx.client)
        if not template_data:
            logger.error("Template for L3 switch %s not found", switch_id)
            continue

        switch_uuid = uuid.UUID(switch_id)
        user_uuid = uuid.UUID(user_id)
        res_uuid = uuid.UUID(reservation_id)

        context = build_context(switch_data, switch_uuid, user_uuid, res_uuid)
        password_keys = extract_password_keys(template_data)
        redacted = redact_context_for_logging(context, password_keys)

        driver_id = uuid.UUID(switch_data["driver_id"])
        driver_sha256 = switch_data.get("driver_sha256", "unknown")
        driver_filename = switch_data.get("driver_filename", "driver.zip")
        connection_type = switch_data.get("connection_type", "Layer 3 Switch")

        async with get_db_session() as db:
            try:
                driver_path = await load_driver(
                    db, driver_id, driver_sha256, driver_filename, connection_type
                )
            except Exception as e:
                logger.error("Failed to load driver for L3 switch %s: %s", switch_id, e)
                continue

            # Login
            login_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "login",
                user_uuid,
                redacted,
                res_uuid,
                dedupe_key=dedupe_key,
            )
            started = datetime.now(timezone.utc)
            login_result = execute_driver_method(
                driver_path, "login", context, password_keys=password_keys
            )
            if login_result["success"]:
                await update_execution_run(
                    db,
                    login_run,
                    "SUCCESS",
                    output=json.dumps(login_result["output"], default=str),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=login_result["duration_ms"],
                )
            else:
                await update_execution_run(
                    db,
                    login_run,
                    "FAILED",
                    error=login_result.get("error"),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=login_result["duration_ms"],
                )
                logger.error("Login failed for L3 switch %s, skipping route operations", switch_id)
                continue

            # One configure_route/remove_route per pinned route, each guarded so
            # a redelivered message only retries the routes that did not succeed.
            # The guard identity is (destination, next_hop, interface) packed
            # into the port_a/port_b columns by _route_run_identity, so ECMP
            # siblings (same prefix + interface, different next hop) stay
            # distinct instead of collapsing into one guarded action.
            switch_routes_ok = True
            for route in routes:
                destination = route.get("destination")
                next_hop = route.get("next_hop")
                interface = route.get("interface")
                ident_a, ident_b = _route_run_identity(destination, next_hop, interface)
                if await action_already_succeeded(
                    db, dedupe_key, switch_uuid, method, ident_a, ident_b
                ):
                    logger.info(
                        "Skipping already-applied %s on switch %s route %s via %s; "
                        "idempotent replay",
                        method,
                        switch_id,
                        destination,
                        interface,
                    )
                    continue
                route_kwargs = {
                    "destination": destination,
                    "next_hop": next_hop,
                    "interface": interface,
                }
                run = await create_execution_run(
                    db,
                    switch_uuid,
                    driver_id,
                    driver_sha256,
                    method,
                    user_uuid,
                    redacted,
                    res_uuid,
                    ident_a,
                    ident_b,
                    method_kwargs=route_kwargs,
                    dedupe_key=dedupe_key,
                )
                op_started = datetime.now(timezone.utc)
                result = execute_driver_method(
                    driver_path,
                    method,
                    context,
                    method_kwargs=route_kwargs,
                    password_keys=password_keys,
                )
                status = "SUCCESS" if result["success"] else "FAILED"
                if not result["success"]:
                    switch_routes_ok = False
                await update_execution_run(
                    db,
                    run,
                    status,
                    output=json.dumps(result["output"], default=str)
                    if result.get("output")
                    else None,
                    error=result.get("error"),
                    started_at=op_started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=result["duration_ms"],
                )

            if l3_action == "deprovision" and switch_routes_ok:
                cleanly_removed.add(switch_id)

            # Logout
            logout_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "logout",
                user_uuid,
                redacted,
                res_uuid,
                dedupe_key=dedupe_key,
            )
            logout_started = datetime.now(timezone.utc)
            logout_result = execute_driver_method(
                driver_path, "logout", context, password_keys=password_keys
            )
            status = "SUCCESS" if logout_result["success"] else "FAILED"
            await update_execution_run(
                db,
                logout_run,
                status,
                output=(
                    json.dumps(logout_result["output"], default=str)
                    if logout_result.get("output")
                    else None
                ),
                error=logout_result.get("error"),
                started_at=logout_started,
                completed_at=datetime.now(timezone.utc),
                duration_ms=logout_result["duration_ms"],
            )

    # Release LAST, and only for switches whose routes were all removed cleanly
    # (see docstring). A transient failure has already raised and NAKed; a
    # driver-result failure leaves that switch out of cleanly_removed so its pin
    # stays ACTIVE as an accurate "routes may still be installed" record.
    if l3_action == "deprovision" and cleanly_removed:
        async with get_db_session() as db:
            for sid in cleanly_removed:
                await release_routes_for_device(db, reservation_id, sid)


# Map NATS events to L3 actions. reservation.failed needs no special flag
# here: deprovision already removes exactly the pinned ACTIVE route_assignments
# and releases each switch's pin only after clean removal, so a reservation
# that failed before pinning tears down nothing (issue #244).
L3_EVENT_ACTIONS = {
    "reservation.created": "provision",
    "reservation.cancelled": "deprovision",
    "reservation.completed": "deprovision",
    "reservation.failed": "deprovision",
}


async def _execute_switch_operations(
    device_ids: list[str],
    action: str,
    reservation_id: str | None,
    user_id: str,
    get_db_session,
    dedupe_key: str | None = None,
    ctx: "_FetchContext | None" = None,
    only_applied_pairs: bool = False,
) -> None:
    """Resolve L1 switch operations for devices and execute driver methods.

    Args:
        device_ids: devices to resolve switch operations for
        action: driver method to call ("connect_ports" or "disconnect_ports")
        reservation_id: reservation UUID string
        user_id: user UUID string
        get_db_session: async context manager that yields an AsyncSession
        dedupe_key: source-message key; a port operation whose SUCCESS run already
            carries it is skipped on redelivery (issue #133).
        ctx: per-event fetch context (shared client + caches, issue #137). When
            None a throwaway context is created so a direct call still works.
        only_applied_pairs: reservation.failed teardown (issue #244). A FAILED
            reservation may have half-provisioned, so only pairs with a SUCCESS
            connect_ports run for this reservation are torn down; a pair that
            was never cross-connected gets no disconnect_ports. With no applied
            pairs on a switch the driver is not even logged into.
    """
    if ctx is None:
        ctx = _FetchContext(None)
    operations = await _resolve_l1_switch_operations(device_ids, ctx)
    if not operations:
        logger.info("No L1 switch operations needed for reservation %s", reservation_id)
        return

    # Import here to avoid circular imports
    from datetime import datetime, timezone

    from app.services.driver_loader import load_driver
    from app.services.driver_sandbox import execute_driver_method
    from app.services.execution_service import (
        action_already_succeeded,
        action_succeeded_for_reservation,
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )

    # Group by switch for batched login/logout
    switch_groups = {}
    for op in operations:
        sid = op["switch_device_id"]
        if sid not in switch_groups:
            switch_groups[sid] = []
        switch_groups[sid].append((op["switch_port_a"], op["switch_port_b"]))

    for switch_id, port_pairs in switch_groups.items():
        switch_data = await ctx.get_device(switch_id)
        if not switch_data:
            logger.error("L1 switch %s not found", switch_id)
            continue

        template_data = await _fetch_template(switch_data.get("template_id", ""), ctx.client)
        if not template_data:
            logger.error("Template for switch %s not found", switch_id)
            continue

        switch_uuid = uuid.UUID(switch_id)
        user_uuid = uuid.UUID(user_id)
        res_uuid = uuid.UUID(reservation_id) if reservation_id else None

        context = build_context(switch_data, switch_uuid, user_uuid, res_uuid)
        password_keys = extract_password_keys(template_data)
        redacted = redact_context_for_logging(context, password_keys)

        driver_id = uuid.UUID(switch_data["driver_id"])
        driver_sha256 = switch_data.get("driver_sha256", "unknown")
        driver_filename = switch_data.get("driver_filename", "driver.zip")
        connection_type = switch_data.get("connection_type", "Layer 1 Switch")

        async with get_db_session() as db:
            # Load driver
            try:
                driver_path = await load_driver(
                    db, driver_id, driver_sha256, driver_filename, connection_type
                )
            except Exception as e:
                logger.error("Failed to load driver for switch %s: %s", switch_id, e)
                continue

            # Idempotency (issue #133): drop port pairs this source message already
            # applied. If a redelivery finds them all done, skip the switch entirely
            # rather than re-login just to do nothing.
            pending_pairs = []
            for port_a, port_b in port_pairs:
                if only_applied_pairs and not await action_succeeded_for_reservation(
                    db, res_uuid, switch_uuid, "connect_ports", port_a, port_b
                ):
                    # Applied-state guard (issue #244): this pair was never
                    # cross-connected for the FAILED reservation, so there is
                    # nothing to tear down on it.
                    logger.info(
                        "Skipping %s on switch %s (%s to %s); connect_ports never "
                        "succeeded for reservation %s",
                        action,
                        switch_id,
                        port_a,
                        port_b,
                        reservation_id,
                    )
                    continue
                if await action_already_succeeded(
                    db, dedupe_key, switch_uuid, action, port_a, port_b
                ):
                    logger.info(
                        "Skipping already-applied %s on switch %s (%s to %s); idempotent replay",
                        action,
                        switch_id,
                        port_a,
                        port_b,
                    )
                    continue
                pending_pairs.append((port_a, port_b))
            if not pending_pairs:
                continue

            # Login
            login_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "login",
                user_uuid,
                redacted,
                res_uuid,
                dedupe_key=dedupe_key,
            )
            started = datetime.now(timezone.utc)
            login_result = execute_driver_method(
                driver_path, "login", context, password_keys=password_keys
            )
            if login_result["success"]:
                await update_execution_run(
                    db,
                    login_run,
                    "SUCCESS",
                    output=json.dumps(login_result["output"], default=str),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=login_result["duration_ms"],
                )
            else:
                await update_execution_run(
                    db,
                    login_run,
                    "FAILED",
                    error=login_result.get("error"),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=login_result["duration_ms"],
                )
                logger.error("Login failed for switch %s, skipping port operations", switch_id)
                continue

            # Execute port operations (already-applied pairs filtered out above)
            for port_a, port_b in pending_pairs:
                port_kwargs = {"port_a": port_a, "port_b": port_b}
                run = await create_execution_run(
                    db,
                    switch_uuid,
                    driver_id,
                    driver_sha256,
                    action,
                    user_uuid,
                    redacted,
                    res_uuid,
                    port_a,
                    port_b,
                    method_kwargs=port_kwargs,
                    dedupe_key=dedupe_key,
                )
                op_started = datetime.now(timezone.utc)
                result = execute_driver_method(
                    driver_path,
                    action,
                    context,
                    method_kwargs=port_kwargs,
                    password_keys=password_keys,
                )
                if result["success"]:
                    await update_execution_run(
                        db,
                        run,
                        "SUCCESS",
                        output=json.dumps(result["output"], default=str),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )
                else:
                    await update_execution_run(
                        db,
                        run,
                        "FAILED",
                        error=result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )

            # Logout
            logout_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "logout",
                user_uuid,
                redacted,
                res_uuid,
                dedupe_key=dedupe_key,
            )
            logout_started = datetime.now(timezone.utc)
            logout_result = execute_driver_method(
                driver_path, "logout", context, password_keys=password_keys
            )
            status = "SUCCESS" if logout_result["success"] else "FAILED"
            await update_execution_run(
                db,
                logout_run,
                status,
                output=(
                    json.dumps(logout_result["output"], default=str)
                    if logout_result.get("output")
                    else None
                ),
                error=logout_result.get("error"),
                started_at=logout_started,
                completed_at=datetime.now(timezone.utc),
                duration_ms=logout_result["duration_ms"],
            )


async def handle_reservation_event(
    event_data: dict, get_db_session, dedupe_key: str | None = None
) -> None:
    """Process a reservation lifecycle event.

    Args:
        event_data: parsed NATS message payload
        get_db_session: async context manager that yields an AsyncSession
        dedupe_key: NATS "<stream>:<sequence>" of the source message, threaded
            into execution_runs so a redelivery skips already-applied driver
            actions (issue #133). None when JetStream metadata is unavailable.
    """
    event_type = event_data.get("event", "")
    action = EVENT_ACTIONS.get(event_type)
    if not action:
        logger.warning("Unknown reservation event type: %s", event_type)
        return

    reservation_id = event_data.get("reservation_id")
    user_id = event_data.get("user_id")

    logger.info(
        "Processing reservation event",
        extra={
            "event": event_type,
            "reservation_id": reservation_id,
        },
    )

    # One httpx client and one set of memoization caches for the whole event, so
    # the L1 and L2 passes pool connections and never re-fetch a device's
    # connections or a shared far-end switch twice (issue #137). For
    # reservation.updated the added and removed device sets are disjoint and
    # touch different connections, but sharing the context is still correct and
    # lets a switch referenced by both reuse one cached device fetch.
    async with httpx.AsyncClient() as client:
        ctx = _FetchContext(client)

        if action == "update_ports":
            added_ids = event_data.get("added_device_ids", [])
            removed_ids = event_data.get("removed_device_ids", [])
            if added_ids:
                await _execute_switch_operations(
                    added_ids,
                    "connect_ports",
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                )
                await _execute_l2_switch_operations(
                    added_ids,
                    "provision",
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                )
                await _execute_l3_switch_operations(
                    added_ids,
                    "provision",
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                )
            if removed_ids:
                await _execute_switch_operations(
                    removed_ids,
                    "disconnect_ports",
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                )
                await _execute_l2_switch_operations(
                    removed_ids,
                    "deprovision",
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                )
                # L3 deprovision on edit is adjacency-aware: a switch still
                # serving a remaining device keeps its routes, so pass the
                # post-edit device set for the still-serving check.
                await _execute_l3_switch_operations(
                    removed_ids,
                    "deprovision",
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                    remaining_device_ids=event_data.get("device_ids", []),
                )
        else:
            device_ids = event_data.get("device_ids", [])
            # reservation.failed teardown (issue #244) is applied-state-only:
            # the reservation may have half-provisioned (or never provisioned),
            # so L1 tears down only pairs whose connect_ports succeeded and L2
            # only what the stored vlan_assignments record. L3's deprovision is
            # already pinned-set-driven, so it needs no flag.
            failed_cleanup = event_type == "reservation.failed"
            await _execute_switch_operations(
                device_ids,
                action,
                reservation_id,
                user_id,
                get_db_session,
                dedupe_key,
                ctx,
                only_applied_pairs=failed_cleanup,
            )
            # L2 switch operations
            l2_action = L2_EVENT_ACTIONS.get(event_type)
            if l2_action:
                await _execute_l2_switch_operations(
                    device_ids,
                    l2_action,
                    reservation_id,
                    user_id,
                    get_db_session,
                    dedupe_key,
                    ctx,
                    failed_cleanup=failed_cleanup,
                )
            # L3 switch operations
            l3_action = L3_EVENT_ACTIONS.get(event_type)
            if l3_action:
                await _execute_l3_switch_operations(
                    device_ids, l3_action, reservation_id, user_id, get_db_session, dedupe_key, ctx
                )

    logger.info(
        "Completed processing reservation event",
        extra={"event": event_type, "reservation_id": reservation_id},
    )


async def _publish_to_dlq(js, payload: bytes) -> None:
    """Publish a message to the reservations DLQ subject. Never raises."""
    try:
        await js.publish(NATS_DLQ_SUBJECT, payload)
    except Exception:
        logger.error("Failed to publish to NATS DLQ", exc_info=True)


async def process_reservation_message(
    msg,
    js,
    handler: Callable[..., Awaitable[None]],
    session_factory: Callable,
    *,
    max_deliver: int = NATS_MAX_DELIVER,
) -> str:
    """Process one NATS message from the herd.reservations.* stream.

    Returns 'ack', 'nak', or 'dlq'. Poison messages (undecodable JSON) are
    routed to the DLQ immediately so a single malformed event does not wedge
    the consumer. Transient handler failures (TransientUpstreamError) NAK so
    JetStream reapplies the configured backoff and redelivers; the handler
    is expected to raise TransientUpstreamError on 5xx or transport errors,
    and swallow 404s as "not found". Once num_delivered reaches max_deliver
    the message is moved to the DLQ (herd.reservations.dlq.execution) for
    inspection and replay. All ack/nak/dlq actions are explicit; the loop
    does not rely on ack_wait timeout to drive failure handling.
    """
    try:
        event_data = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.error(
            "Poison message on reservations stream; routing to DLQ",
            extra={"action": "nats_poison_message", "size": len(msg.data)},
        )
        # Undecodable messages go to DLQ immediately and are ACK'd so they don't loop.
        await _publish_to_dlq(js, msg.data)
        await msg.ack()
        return "dlq"

    try:
        # Key idempotency on the stable producer-stamped event_id when present
        # (issue #21), so a relay republish under a new stream sequence still
        # dedupes; fall back to "<stream>:<sequence>" for pre-outbox events.
        await handler(event_data, session_factory, event_dedupe_key(event_data, msg))
    except PermanentEventError as exc:
        # Non-retryable (e.g. VLAN pool exhausted): the in-use set is unchanged
        # between attempts, so retrying only burns max_deliver and delays the DLQ.
        # Route to the DLQ on the FIRST delivery with a distinct exhaustion phrase.
        num_delivered = getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
        logger.error(
            "Permanent error (VLAN exhaustion) processing NATS message; "
            "routing to DLQ on first delivery (no retry)",
            extra={
                "action": "nats_dlq_permanent",
                "delivered": num_delivered,
                "event": event_data.get("event"),
            },
            exc_info=exc,
        )
        await _publish_to_dlq(js, msg.data)
        await msg.ack()
        return "dlq"
    except Exception as exc:
        num_delivered = getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
        if num_delivered >= max_deliver:
            # Backoff expired: move to DLQ (4-token subject sits outside the 3-token
            # consumer filter so it is not redelivered to this consumer, avoiding
            # poison loops). DLQ retention allows inspection and manual replay.
            logger.error(
                "Message exhausted max_deliver; routing to DLQ",
                extra={
                    "action": "nats_dlq_exhausted",
                    "delivered": num_delivered,
                    "event": event_data.get("event"),
                },
                exc_info=exc,
            )
            await _publish_to_dlq(js, msg.data)
            await msg.ack()
            return "dlq"
        # Transient error: NAK so JetStream applies the backoff delay and redelivers.
        # This signals the handler that resource contention or a service outage might
        # clear soon, so do not give up yet.
        logger.warning(
            "Transient error processing NATS message; NAK for retry",
            extra={
                "action": "nats_message_nak",
                "delivered": num_delivered,
                "event": event_data.get("event"),
            },
            exc_info=exc,
        )
        await msg.nak()
        return "nak"

    await msg.ack()
    return "ack"


async def start_nats_consumer(app) -> None:
    """Start the NATS consumer as a background task during app lifespan.

    Subscribes to "herd.reservations.*" events with a durable consumer, configures
    bounded retry policy (max_deliver + exponential backoff), and spins the message
    loop. Failed messages that exhaust retries or are poison (undecodable JSON) are
    routed to the DLQ stream for inspection and manual replay. The consumer survives
    pod restarts; restarting the service resumes consuming from the last ACK'd offset.
    """
    import nats
    from nats.js.api import ConsumerConfig

    try:
        # Retry reconnect forever so the durable consumer and the health-event
        # outbox relay resume after a broker restart instead of giving up at the
        # default 60-attempt cap.
        nc = await nats.connect(
            settings.nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        app.state.nats = nc
        js = nc.jetstream()

        # Ensure stream exists (idempotent; no-op if already created by reservations service)
        try:
            await js.add_stream(
                name="HERD_RESERVATIONS",
                subjects=["herd.reservations.*"],
            )
        except Exception:
            logger.warning("Could not create/update NATS stream", exc_info=True)

        # Durable consumer with explicit retry policy: bounded redelivery + backoff.
        # Durability persists the consumer state across restarts, so unprocessed messages
        # are not lost if the execution pod goes down. The consumer name is scoped to
        # this service so other consumers (e.g., notifications) have their own durable
        # offset. The DLQ subject (NATS_DLQ_SUBJECT) is intentionally outside this filter
        # so DLQ'd messages are not redelivered to this consumer; see NATS_DLQ_SUBJECT.
        psub = await js.pull_subscribe(
            "herd.reservations.*",
            durable="execution-consumer",
            config=ConsumerConfig(
                max_deliver=NATS_MAX_DELIVER,
                ack_wait=NATS_ACK_WAIT_SECONDS,
                backoff=NATS_BACKOFF_SECONDS,
            ),
        )

        from app.database import AsyncSessionLocal

        def _get_db_session():
            """Context manager for database sessions in the NATS consumer."""

            class _SessionCtx:
                async def __aenter__(self):
                    self._session = AsyncSessionLocal()
                    return self._session

                async def __aexit__(self, *args):
                    await self._session.close()

            return _SessionCtx()

        async def _consumer_loop():
            while True:
                try:
                    msgs = await psub.fetch(NATS_FETCH_BATCH, timeout=NATS_FETCH_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    raise
                except (nats.errors.TimeoutError, asyncio.TimeoutError):
                    # No messages this cycle; fetch again. The fetch also
                    # re-establishes delivery after a broker reconnect, which a
                    # push subscription does not do reliably (issue #21).
                    continue
                except Exception:
                    # Connection lost or reconnecting; pause then re-fetch.
                    logger.warning("NATS pull fetch failed; will retry", exc_info=True)
                    await asyncio.sleep(NATS_FETCH_TIMEOUT_SECONDS)
                    continue
                for msg in msgs:
                    try:
                        await process_reservation_message(
                            msg, js, handle_reservation_event, _get_db_session
                        )
                    except Exception:
                        # process_reservation_message never raises on its own; this
                        # catches ack/nak/publish failures so the loop keeps draining.
                        logger.error("Unexpected error in NATS consumer loop", exc_info=True)

        app.state.nats_consumer_task = asyncio.create_task(_consumer_loop())
        logger.info("NATS consumer started")

    except Exception:
        logger.warning(
            "Failed to connect to NATS; operating without event-driven execution",
            exc_info=True,
        )


async def stop_nats_consumer(app) -> None:
    """Stop the NATS consumer and close the connection."""
    task = getattr(app.state, "nats_consumer_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    nc = getattr(app.state, "nats", None)
    if nc:
        try:
            await nc.close()
        except Exception:
            logger.error("Error closing NATS connection", exc_info=True)
