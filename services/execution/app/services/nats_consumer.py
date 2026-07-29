"""NATS consumer: subscribe to reservation lifecycle events and trigger driver execution."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

import httpx
from herd_common.outbox import event_dedupe_key
from herd_common.retry import retry_with_backoff
from sqlalchemy import select

from app.config import settings
from app.services.execution_service import driver_result_failed
from app.services.health_scheduler import apply_reservation_event_tiers

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
# Work-in-progress heartbeat cadence (issue #317). A provisioning handler runs
# the driver sandbox for up to recipe_timeout_seconds (300s), far beyond
# ack_wait (30s). While a message is being processed (or waits its turn behind a
# slow one in the same fetch batch), the loop resets its ack timer on this
# interval so JetStream does not redeliver a message that is still in flight and
# double-execute its provisioning, possibly on a peer replica. Half of ack_wait
# leaves margin for a late heartbeat. A crashed consumer stops heartbeating, so
# ack_wait still expires and the message correctly redelivers.
NATS_HEARTBEAT_SECONDS = NATS_ACK_WAIT_SECONDS // 2
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

# Event name for the connection-driven L1 reconcile (ADR 0007, issue #345 P3b).
WIRING_CHANGED_EVENT = "reservation.wiring_changed"
# Per-connection in-line retry for a transient driver failure while applying one
# cross-connect (ADR 0007 Decision 6 item 1). Mirrors run_driver_action's discipline
# (herd_common.retry.retry_with_backoff): a bounded number of attempts with
# exponential backoff, then the connection is parked FAILED and the pass continues.
# A per-connection DRIVER failure never NAKs the message (Decision 7); only an
# UPSTREAM cabling/inventory failure does. Delays are small so a failing connection
# does not blow the integration suite's per-test timeout budget.
WIRING_DRIVER_ATTEMPTS = 3
WIRING_DRIVER_INITIAL_DELAY = 0.2
WIRING_DRIVER_BACKOFF_FACTOR = 2.0
WIRING_DRIVER_MAX_DELAY = 2.0
# Reason pinned on a FAILED row when a recorded hop no longer resolves to a live
# switch/port (ADR 0007 Decision 5): execution applies recorded hops verbatim and
# never re-routes, so a graph change between save and apply strands the connection.
WIRING_UNRESOLVABLE_REASON = "recorded hop unresolvable"
# Reason pinned when a set of recorded hops does not form a simple chain (a switch
# touched by more than two hops in one apply, or a cycle). The switch cross-connect
# pairing is then not recoverable from the flattened wire delta (the pairing lived in
# the canvas edge, which the delta discards), so the reconcile refuses to guess and
# fails those hops rather than risk a physical mis-cross-connect (ADR 0007 review).
WIRING_NOT_SIMPLE_CHAIN_REASON = "hop set is not a simple chain"


async def _run_sandbox(*args, **kwargs):
    """Run the synchronous driver sandbox off the event loop (issue #317).

    execute_driver_method blocks on subprocess.run for up to
    recipe_timeout_seconds. Running it inline on the consumer's event-loop task
    stalls the loop, starving the outbox relay, the health scheduler, and the
    per-message ack heartbeat, and serializes every replica's provisioning on one
    thread. This mirrors execution_service.run_driver_action's to_thread wrap
    (added for the health path in #306); the consumer manages its own execution
    runs, so it calls the sandbox directly rather than through run_driver_action.
    Imported lazily to match the module's existing driver_sandbox import style.
    """
    from app.services.driver_sandbox import execute_driver_method

    return await asyncio.to_thread(execute_driver_method, *args, **kwargs)


async def _keep_messages_alive(messages: list, interval: float) -> None:
    """Reset ack_wait on every still-in-flight message until it is settled.

    Runs concurrently with the sequential batch processing (issue #317). Each
    cycle sends work-in-progress to every message still in `messages`; the loop
    removes a message as soon as it is acked/naked, so heartbeating stops for
    settled messages. in_progress failures are swallowed: a heartbeat is
    best-effort and must never wedge the consumer. Requires the driver calls to
    run off-loop (see _run_sandbox), or this task could never get scheduled while
    a provisioning handler holds the loop.
    """
    while True:
        await asyncio.sleep(interval)
        for msg in list(messages):
            try:
                await msg.in_progress()
            except Exception:
                logger.debug("in_progress heartbeat failed; continuing", exc_info=True)


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


async def _fetch_hypervisor(hypervisor_id: str, client=None) -> dict | None:
    """Fetch a hypervisor's internal record from the inventory service (issue #32).

    Mirrors _fetch_template exactly: raises TransientUpstreamError on a 5xx or
    transport error so the message NAKs and retries; a non-200 below 500 (a
    genuine 404) returns None, which the create flow treats as a permanent
    config error.
    """
    url = f"{settings.inventory_service_url}/hypervisors/{hypervisor_id}/internal"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch hypervisor {hypervisor_id}",
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json()


async def _fetch_secret_value(secret_id: str, client=None) -> dict | None:
    """Fetch a secret's decrypted data dict from the secrets service (issue #32).

    Reads GET /internal/secrets/{id}/value (X-Internal-Token) and returns the
    `data` mapping of secret key-values. Raises TransientUpstreamError on a 5xx
    or transport error so the message NAKs and retries; a genuine 404 returns
    None (a permanent config error to the create flow). Mirrors the _fetch_*
    idiom exactly.
    """
    url = f"{settings.secrets_service_url}/internal/secrets/{secret_id}/value"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch secret {secret_id}",
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("data", {}) if isinstance(data, dict) else {}


async def _fetch_fork_intended_wires(reservation_id: str, client=None) -> list[dict]:
    """Fetch a reservation fork's full intended L1 wiring from cabling (ADR 0007).

    The desired set for the gap/heal full-reconcile path (Decision 4): cabling's
    internal fork GET returns every fork_connections row, the intended wiring as the
    human reviewed and saved it. Raises TransientUpstreamError on a 5xx or transport
    error so the message NAKs (an UPSTREAM failure, Decision 7); a 404 (no fork yet)
    returns [] so the reconcile converges the applied set to empty. Only L1 rows are
    returned; phase 1 wiring is L1 by construction, and a stray non-L1 row is ignored.
    """
    url = f"{settings.cabling_service_url}/internal/forks/{reservation_id}"
    async with _client_ctx(client) as c:
        resp = await _get_internal(
            c,
            url,
            what=f"fetch fork wiring for reservation {reservation_id}",
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            logger.warning(
                "Unexpected status fetching fork wiring for reservation %s: %s",
                reservation_id,
                resp.status_code,
            )
            return []
        data = resp.json()
        connections = data.get("connections", []) if isinstance(data, dict) else []
        return [c for c in connections if (c.get("layer") or "L1") == "L1"]


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

    if not operations:
        return []

    # Pair per switch, not by array position (issue #366). The raw connections
    # graph fetched above carries no edge_key (that exists only on fork
    # connections, ADR 0007), so the intended grouping of hops into logical
    # links is unavailable here. What IS sound without it: a switch touched by
    # exactly two reserved ports has exactly one possible cross-connect, so
    # that pair is unambiguous under any device_ids order (including parallel
    # paths where the flattened graph forms a cycle, which a global chain walk
    # would wrongly refuse). A switch touched by any other number of reserved
    # ports is ambiguous at this layer and is skipped with a warning instead
    # of positionally guessed: the silent mis-cross-connect was the defect,
    # and a fork save wires the ambiguous case correctly via edge_key until
    # ADR 0009 phase 7 retires this resolver entirely.
    ops_by_switch: dict[str, list[dict]] = {}
    for op in operations:
        ops_by_switch.setdefault(op["switch_device_id"], []).append(op)

    paired = []
    for switch_id, hops in ops_by_switch.items():
        if len(hops) == 2:
            paired.append(
                {
                    "switch_device_id": switch_id,
                    "switch_port_a": hops[0]["switch_port"],
                    "switch_port_b": hops[1]["switch_port"],
                }
            )
        else:
            logger.warning(
                "Switch %s has %d reserved-adjacent ports; pairing is ambiguous "
                "without edge intent (issue #366), skipping its cross-connects. "
                "A fork save wires them from the canvas edges.",
                switch_id,
                len(hops),
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
    from app.models.vlan_assignment import VlanAssignment
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

    # Assign a VLAN per fabric, and read back its allocation row id so the legacy path can
    # record memberships into l2_port_assignments during the phase 4-6 transition overlap.
    fabric_vlan: dict[uuid.UUID, int] = {}
    fabric_va_id: dict[uuid.UUID, uuid.UUID] = {}
    async with get_db_session() as db:
        for fid, sids in fabric_switches.items():
            vlan_id = await find_or_assign_vlan(db, reservation_id, fid, sids)
            fabric_vlan[fid] = vlan_id
            row = (
                await db.execute(
                    select(VlanAssignment).where(
                        VlanAssignment.reservation_id == uuid.UUID(reservation_id),
                        VlanAssignment.fabric_id == fid,
                        VlanAssignment.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                fabric_va_id[fid] = row.id

    # Set vlan_id and the allocation row id on each operation
    for op in operations:
        fid = switch_fabric[op["switch_device_id"]]
        op["vlan_id"] = fabric_vlan[fid]
        op["vlan_assignment_id"] = fabric_va_id.get(fid)

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
        switch_va_id: dict[str, uuid.UUID] = {}
        for a in assignments:
            for sid in a.switch_device_ids:
                switch_vlan[sid] = a.vlan_id
                switch_va_id[sid] = getattr(a, "id", None)
        operations = [op for op in operations if op["switch_device_id"] in switch_vlan]
        for op in operations:
            op["vlan_id"] = switch_vlan[op["switch_device_id"]]
            op["vlan_assignment_id"] = switch_va_id.get(op["switch_device_id"])
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
            switch_va_id = {}
            for a in assignments:
                for sid in a.switch_device_ids:
                    switch_vlan[sid] = a.vlan_id
                    switch_va_id[sid] = getattr(a, "id", None)
            for op in operations:
                sid = op["switch_device_id"]
                op["vlan_id"] = switch_vlan.get(sid, _derive_vlan_id(reservation_id))
                op["vlan_assignment_id"] = switch_va_id.get(sid)
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
    from app.services.execution_service import (
        action_already_succeeded,
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.l2_membership_service import (
        is_membership_active,
        record_l2_failed,
        record_l2_membership_active,
        release_l2_membership,
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
            login_result = await _run_sandbox(
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
                    result = await _run_sandbox(
                        driver_path,
                        "create_vlan",
                        context,
                        method_kwargs=vlan_kwargs,
                        password_keys=password_keys,
                    )
                    # Gate on the driver RESULT payload, not only the transport-level
                    # sandbox flag (issue #393, the L2 analogue of #370): a driver
                    # returning success=False without raising is a failure the raw
                    # transport flag misses.
                    op_failed, op_error = driver_result_failed(result)
                    status = "FAILED" if op_failed else "SUCCESS"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=op_error if op_failed else result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )

                # Add each port to the VLAN
                for op in ops:
                    port = op["switch_port"]
                    op_va_id = op.get("vlan_assignment_id")
                    # Ledger-ACTIVE idempotency (phase 4-6 transition overlap): a membership
                    # this reservation already holds ACTIVE (applied by the wiring_changed
                    # reconcile or a prior legacy pass) needs no driver add. Mirrors the L1
                    # is_pair_active gate; keeps the two provisioning paths idempotent.
                    if op_va_id is not None and await is_membership_active(
                        db, res_uuid, switch_uuid, port
                    ):
                        logger.info(
                            "Skipping add_to_vlan on switch %s port %s; membership already "
                            "ACTIVE in ledger",
                            switch_id,
                            port,
                        )
                        continue
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
                    result = await _run_sandbox(
                        driver_path,
                        "add_to_vlan",
                        context,
                        method_kwargs=port_kwargs,
                        password_keys=password_keys,
                    )
                    op_failed, op_error = driver_result_failed(result)
                    status = "FAILED" if op_failed else "SUCCESS"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=op_error if op_failed else result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )
                    # Record the driver-gated outcome into the membership ledger so it stays
                    # complete whichever path touched hardware (result-gated, same rules as
                    # the reconcile). A None allocation id means the readback in
                    # _assign_vlans_to_operations failed; skip the ledger write rather than
                    # key a membership to nothing.
                    if op_va_id is not None:
                        if op_failed:
                            await record_l2_failed(
                                db,
                                res_uuid,
                                op_va_id,
                                switch_uuid,
                                port,
                                1,
                                op_error,
                                intended="ACTIVE",
                            )
                        else:
                            await record_l2_membership_active(
                                db, res_uuid, op_va_id, switch_uuid, port
                            )

            elif l2_action == "deprovision":
                # Remove each port from the VLAN first
                for op in ops:
                    port = op["switch_port"]
                    op_va_id = op.get("vlan_assignment_id")
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
                    result = await _run_sandbox(
                        driver_path,
                        "remove_from_vlan",
                        context,
                        method_kwargs=port_kwargs,
                        password_keys=password_keys,
                    )
                    op_failed, op_error = driver_result_failed(result)
                    status = "FAILED" if op_failed else "SUCCESS"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=op_error if op_failed else result.get("error"),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )
                    # Record the driver-gated teardown outcome into the membership ledger
                    # (result-gated): a successful remove releases the membership; a failed
                    # remove parks it FAILED intended RELEASED so the retry channel can
                    # finish the teardown (previously silent, issue #369 for L2).
                    if op_va_id is not None:
                        if op_failed:
                            await record_l2_failed(
                                db,
                                res_uuid,
                                op_va_id,
                                switch_uuid,
                                port,
                                1,
                                op_error,
                                intended="RELEASED",
                            )
                        else:
                            await release_l2_membership(db, res_uuid, switch_uuid, port)

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
                    result = await _run_sandbox(
                        driver_path,
                        "delete_vlan",
                        context,
                        method_kwargs=vlan_kwargs,
                        password_keys=password_keys,
                    )
                    op_failed, op_error = driver_result_failed(result)
                    status = "FAILED" if op_failed else "SUCCESS"
                    await update_execution_run(
                        db,
                        run,
                        status,
                        output=json.dumps(result["output"], default=str)
                        if result.get("output")
                        else None,
                        error=op_error if op_failed else result.get("error"),
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
            logout_result = await _run_sandbox(
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
        get_effective_pinned_routes,
        get_route_assignments,
        record_route_active,
        record_route_failed,
        release_routes_for_device,
    )

    # Build the per-switch route work. For provision the pinned set is an existing
    # non-RELEASED pin reused verbatim if one survives (issue #20), else the switch's
    # latest config version; the pin's STATUS is now gated on the driver outcome (ADR
    # 0009 phase 5), so the ACTIVE/FAILED row is written AFTER the routes are driven, not
    # up front. For deprovision the routes come exclusively from the stored ACTIVE pins.
    switch_routes: dict[str, list[dict]] = {}

    if l3_action == "provision":
        switch_ids = await _resolve_l3_switch_operations(device_ids, ctx)
        if not switch_ids:
            logger.info("No L3 switch operations needed for reservation %s", reservation_id)
            return
        async with get_db_session() as db:
            for sid in switch_ids:
                pinned = await get_effective_pinned_routes(db, reservation_id, sid)
                if pinned is None:
                    detail = await ctx.get_latest_config(sid)
                    pinned = ((detail or {}).get("config") or {}).get("routes") or []
                    if not pinned:
                        logger.info(
                            "L3 switch %s has no routes in its latest config version; "
                            "skipping for reservation %s",
                            sid,
                            reservation_id,
                        )
                        continue
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
            login_result = await _run_sandbox(
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
            switch_attempts = 0
            switch_last_error: str | None = None
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
                result = await _run_sandbox(
                    driver_path,
                    method,
                    context,
                    method_kwargs=route_kwargs,
                    password_keys=password_keys,
                )
                # Gate on the driver RESULT payload (issue #393, the L3 analogue of
                # #370): a configure_route/remove_route that returns success=False
                # without raising must keep switch_routes_ok False, so a semantically
                # failed remove_route keeps this switch's route pin ACTIVE below
                # (same posture as an existing transport failure).
                op_failed, op_error = driver_result_failed(result)
                status = "FAILED" if op_failed else "SUCCESS"
                if op_failed:
                    switch_routes_ok = False
                    switch_attempts += 1
                    switch_last_error = op_error
                await update_execution_run(
                    db,
                    run,
                    status,
                    output=json.dumps(result["output"], default=str)
                    if result.get("output")
                    else None,
                    error=op_error if op_failed else result.get("error"),
                    started_at=op_started,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=result["duration_ms"],
                )

            if l3_action == "deprovision" and switch_routes_ok:
                cleanly_removed.add(switch_id)

            # Result-gated pin write for the legacy provision path (ADR 0009 phase 5
            # transition overlap): a clean provision pins the switch ACTIVE (idempotent
            # under redelivery, and recognized by the fork-driven reconcile so it never
            # re-provisions a legacy-pinned switch); any route failure lands a FAILED pin
            # intended ACTIVE so the retry channel reattempts the PINNED set rather than
            # a bogus ACTIVE row claiming routes the hardware never took. The legacy
            # DEPROVISION keeps its release-after-clean-removal ledger handling below
            # unchanged (its failure-keeps-ACTIVE posture and the #244 stranded-teardown
            # reads are ADR 0009 phase 6 territory).
            if l3_action == "provision":
                if switch_routes_ok:
                    await record_route_active(db, reservation_id, switch_id, routes)
                else:
                    await record_route_failed(
                        db,
                        reservation_id,
                        switch_id,
                        routes,
                        switch_attempts or 1,
                        switch_last_error,
                        intended="ACTIVE",
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
            logout_result = await _run_sandbox(
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
    from app.services.execution_service import (
        action_already_succeeded,
        action_succeeded_for_reservation,
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.l1_assignment_service import (
        record_l1_connect,
        record_l1_failed,
        release_l1_connection,
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
            login_result = await _run_sandbox(
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
                result = await _run_sandbox(
                    driver_path,
                    action,
                    context,
                    method_kwargs=port_kwargs,
                    password_keys=password_keys,
                )
                # Inspect the driver RESULT payload, not just the transport-level
                # sandbox success (#345 phase 4 live-gate): a driver that returns
                # success=False without raising is a failure the sandbox flag misses,
                # and previously recorded a false SUCCESS run plus a phantom ACTIVE row.
                op_failed, op_error = driver_result_failed(result)
                if not op_failed:
                    await update_execution_run(
                        db,
                        run,
                        "SUCCESS",
                        output=json.dumps(result["output"], default=str),
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )
                    # Connection-addressable applied state (ADR 0007 Decision 4,
                    # issue #345 P3b phase 1). A new projection alongside the
                    # unchanged execution_runs audit log: a successful connect
                    # records an ACTIVE row, a successful disconnect releases it.
                    if res_uuid is not None:
                        if action == "connect_ports":
                            await record_l1_connect(db, res_uuid, switch_uuid, port_a, port_b)
                        elif action == "disconnect_ports":
                            await release_l1_connection(db, res_uuid, switch_uuid, port_a, port_b)
                else:
                    await update_execution_run(
                        db,
                        run,
                        "FAILED",
                        error=op_error,
                        started_at=op_started,
                        completed_at=datetime.now(timezone.utc),
                        duration_ms=result["duration_ms"],
                    )
                    # A failed connect or disconnect lands a FAILED assignment row so
                    # the connection is visible and retryable through the wiring
                    # retry channel (issue #369): intended records which direction
                    # this attempt was making, so a failed teardown parks FAILED
                    # with intended RELEASED rather than silently leaving the row
                    # ACTIVE with no path to retry the disconnect. The issue #412
                    # guard (record_l1_failed) still refuses to downgrade a row a
                    # concurrent BUILD already won; it does not apply here.
                    if res_uuid is not None:
                        await record_l1_failed(
                            db,
                            res_uuid,
                            switch_uuid,
                            port_a,
                            port_b,
                            1,
                            op_error or "driver reported failure",
                            intended="ACTIVE" if action == "connect_ports" else "RELEASED",
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
            logout_result = await _run_sandbox(
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


# --- Dynamic resources (ADR 0004, issue #32) --------------------------------
#
# A recipe is an ordinary driver package with connection_type "Hypervisor". The
# create flow handles the new reservation.provision_requested event: for each
# requested instance it loads the recipe, runs login/create_instance/logout in
# the sandbox, materializes the result as an inventory device, and records the
# outcome in the dynamic_instances ledger. Teardown drives from that ledger on
# the lifecycle events, the peer of the L2/L3 applied-state teardown. This whole
# path is inert until phase 3 publishes provision_requested; unknown events keep
# flowing exactly as before.

# Lifecycle events that tear down dynamic instances of the whole reservation.
# reservation.updated is handled separately (only removed devices), so it is not
# in this set.
DYNAMIC_TEARDOWN_EVENTS = {
    "reservation.completed",
    "reservation.cancelled",
    "reservation.failed",
}


def _build_recipe_context(
    template: dict,
    hypervisor: dict,
    secret_data: dict,
    request_id: str,
    reservation_id: str,
    user_id: str,
) -> tuple[dict, set[str]]:
    """Build the sandbox context for a Hypervisor recipe and its secret keys.

    Template field defaults arrive as the usual HERD_<field> keys (there is no
    device row yet, so defaults stand in for field_data). The recipe also gets
    the hypervisor endpoint/type and the request/reservation/user ids.
    HERD_request_id is deterministic per requested instance, so a recipe MUST
    name its hypervisor-side resources from it: the redelivery idempotency story
    relies on a retried create naming the same instance rather than leaking a
    duplicate. Every secret value is merged under HERD_secret_<key>, and all such
    keys are returned so the caller can list them in password_keys; they then
    travel only in the context temp file, never the child environment.
    """
    context: dict = {}
    for section in template.get("sections", []):
        for field in section.get("fields", []):
            key = field.get("key")
            if key is None:
                continue
            context[f"HERD_{key}"] = field.get("default")

    context["HERD_hypervisor_endpoint"] = hypervisor.get("endpoint")
    context["HERD_hypervisor_type"] = hypervisor.get("hypervisor_type")
    context["HERD_request_id"] = str(request_id)
    context["HERD_reservation_id"] = str(reservation_id)
    context["HERD_user_id"] = str(user_id)

    secret_keys: set[str] = set()
    for key, value in (secret_data or {}).items():
        ctx_key = f"HERD_secret_{key}"
        context[ctx_key] = value
        secret_keys.add(ctx_key)
    return context, secret_keys


async def _run_recipe_step(
    db,
    device_id: uuid.UUID,
    driver_id: uuid.UUID,
    driver_sha256: str,
    action: str,
    user_id: uuid.UUID,
    redacted: dict,
    reservation_id: uuid.UUID,
    driver_path: str,
    context: dict,
    password_keys: set[str],
    *,
    dedupe_key: str | None = None,
    method_kwargs: dict | None = None,
) -> dict:
    """Run one recipe method in the sandbox, recording an ExecutionRun row.

    The same login/op/logout ExecutionRun bookkeeping the L1/L2/L3 flows do, so
    recipe actions are auditable alongside physical provisioning. There is no
    device yet for a create, so device_id is the hypervisor id: the recipe acts
    on the hypervisor, and create and teardown runs group under it. Recipe steps
    get the long recipe timeout, not the 30s driver default.
    """
    from datetime import datetime, timezone

    from app.services.execution_service import create_execution_run, update_execution_run

    run = await create_execution_run(
        db,
        device_id,
        driver_id,
        driver_sha256,
        action,
        user_id,
        redacted,
        reservation_id,
        method_kwargs=method_kwargs,
        dedupe_key=dedupe_key,
    )
    started = datetime.now(timezone.utc)
    result = await _run_sandbox(
        driver_path,
        action,
        context,
        method_kwargs=method_kwargs,
        password_keys=password_keys,
        timeout=settings.recipe_timeout_seconds,
    )
    status = "SUCCESS" if result["success"] else "FAILED"
    await update_execution_run(
        db,
        run,
        status,
        output=json.dumps(result["output"], default=str) if result.get("output") else None,
        error=result.get("error"),
        started_at=started,
        completed_at=datetime.now(timezone.utc),
        duration_ms=result["duration_ms"],
    )
    return result


def _recipe_reported_success(result: dict) -> bool:
    """True when both the sandbox ran and the recipe's own success flag is set.

    Built on the shared ``driver_result_failed`` rule with one STRICTER delta:
    create_instance and destroy_instance must positively acknowledge with
    {"success": True, ...}, so a missing ``success`` key counts as failure
    here, where ``driver_result_failed``'s bare-data posture counts it as
    success. The delta is deliberate; do not swap one helper for the other
    (a recipe that never acknowledges an instance create must not be treated
    as provisioned). Login/logout carry no such flag, so callers check
    result["success"] directly for those.
    """
    failed, _ = driver_result_failed(result)
    if failed:
        return False
    output = result.get("output")
    return isinstance(output, dict) and bool(output.get("success"))


async def _create_dynamic_device(
    client, template_id: str, reservation_id: str, field_data: dict, request_id: str | None = None
) -> dict | None:
    """POST /devices/internal to materialize an instance as a device.

    Returns the created device dict (with its id) on 201. Raises
    TransientUpstreamError on a 5xx or transport error so the message NAKs; a
    4xx returns None, which the caller treats as a permanent config error.

    request_id is the booking's dynamic-request id and makes the create
    idempotent (issue #275): a redelivered provision_requested re-posts the same
    request_id, so inventory returns the already-materialized device row instead
    of creating a second one that the ledger would orphan.
    """
    url = f"{settings.inventory_service_url}/devices/internal"
    body = {
        "template_id": template_id,
        "reservation_id": reservation_id,
        "field_data": field_data,
        "request_id": request_id,
    }
    try:
        resp = await client.post(
            url,
            json=body,
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise TransientUpstreamError(f"create dynamic device: transport error: {exc}") from exc
    if resp.status_code >= 500:
        raise TransientUpstreamError(f"create dynamic device: upstream {resp.status_code}")
    if resp.status_code == 201:
        return resp.json()
    logger.error(
        "Inventory rejected dynamic device create for template %s: %s",
        template_id,
        resp.status_code,
    )
    return None


async def _delete_dynamic_device(client, device_id: str) -> bool:
    """DELETE /devices/{id}/internal. True on 204 or 404 (already gone).

    Raises TransientUpstreamError on a 5xx or transport error so the teardown
    NAKs. An unexpected 4xx returns False; the caller leaves the ledger row
    ACTIVE rather than falsely recording the instance as destroyed.
    """
    url = f"{settings.inventory_service_url}/devices/{device_id}/internal"
    try:
        resp = await client.delete(
            url,
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise TransientUpstreamError(
            f"delete dynamic device {device_id}: transport error: {exc}"
        ) from exc
    if resp.status_code >= 500:
        raise TransientUpstreamError(
            f"delete dynamic device {device_id}: upstream {resp.status_code}"
        )
    if resp.status_code in (204, 404):
        return True
    logger.warning("Unexpected status %s deleting dynamic device %s", resp.status_code, device_id)
    return False


async def _post_provision_result(
    client, reservation_id: str, *, succeeded: bool, device_ids: list[str], error: str | None
) -> None:
    """POST the provision-result callback to reservations; raise on any failure."""
    url = f"{settings.reservations_service_url}/internal/{reservation_id}/provision-result"
    body = {"succeeded": succeeded, "device_ids": device_ids, "error": error}
    resp = await client.post(
        url,
        json=body,
        headers={"X-Internal-Token": settings.internal_api_token},
        timeout=10.0,
    )
    resp.raise_for_status()


async def _post_provision_result_best_effort(
    reservation_id: str, *, succeeded: bool, device_ids: list[str], error: str | None
) -> None:
    """Post the provision-result callback with retry-then-log (3 attempts).

    Mirrors the fork-hook precedent. If the callback still fails after retries
    it is logged and swallowed: the reservations-side timeout backstop
    (provision_timeout_seconds) transitions a stuck PENDING_PROVISION
    reservation to FAILED, so a lost callback does not strand the reservation.
    """
    async with httpx.AsyncClient() as client:
        try:
            await retry_with_backoff(
                lambda: _post_provision_result(
                    client,
                    reservation_id,
                    succeeded=succeeded,
                    device_ids=device_ids,
                    error=error,
                ),
                attempts=3,
            )
        except Exception:
            logger.error(
                "provision-result callback failed after retries for reservation %s; "
                "relying on the reservations timeout backstop",
                reservation_id,
                exc_info=True,
            )


async def _maybe_post_provision_failure(event_data: dict, reason: str) -> None:
    """Best-effort failure callback for a dead-lettered provision_requested.

    Only fires for reservation.provision_requested. Reservations responds to the
    failure callback by transitioning to FAILED and publishing reservation.failed,
    whose teardown handler owns instance cleanup, so we deliberately do NOT tear
    down already-created instances here (that would race the teardown handler).
    Any error is swallowed; the timeout backstop covers a lost callback.
    """
    if event_data.get("event") != "reservation.provision_requested":
        return
    reservation_id = event_data.get("reservation_id")
    if not reservation_id:
        return
    async with httpx.AsyncClient() as client:
        try:
            await _post_provision_result(
                client, reservation_id, succeeded=False, device_ids=[], error=reason
            )
        except Exception:
            logger.error(
                "Best-effort provision-failure callback failed for reservation %s",
                reservation_id,
                exc_info=True,
            )


async def _fetch_recipe_deps(
    template_id: str, hypervisor_id: str | None, client
) -> tuple[dict | None, dict | None, dict | None]:
    """Fetch (template, hypervisor, secret_data) for a recipe run.

    Any of the three being None means a 404 (a missing config resource); the
    caller decides whether that is a permanent create-flow error or a teardown
    that should leave the row ACTIVE. A 5xx or transport error has already raised
    TransientUpstreamError inside the fetch helpers.
    """
    template = await _fetch_template(template_id, client)
    if template is None:
        return None, None, None
    if hypervisor_id is None:
        hypervisor_id = template.get("hypervisor_id")
    if not hypervisor_id:
        return template, None, None
    hypervisor = await _fetch_hypervisor(hypervisor_id, client)
    if hypervisor is None:
        return template, None, None
    secret = await _fetch_secret_value(hypervisor.get("secret_id"), client)
    return template, hypervisor, secret


async def _provision_one_instance(
    req: dict,
    reservation_id: str,
    user_id: str,
    get_db_session,
    dedupe_key: str | None,
    client,
) -> str:
    """Create one dynamic instance and return its materialized device id.

    Idempotent on the ledger: a redelivery that finds the row already ACTIVE with
    a device skips the whole create. A missing template/hypervisor/secret (404),
    or a structurally broken recipe package that can never load, is a
    PermanentEventError (retry cannot heal); a driver-result failure or sandbox
    error raises so the message NAKs with the row left CREATING for an idempotent
    retry.
    """
    from app.services.driver_loader import DriverPackageError, load_driver
    from app.services.dynamic_instance_service import (
        get_by_request_id,
        insert_or_get_creating,
        mark_active,
        set_instance_ref,
    )
    from app.services.execution_service import (
        extract_password_keys,
        redact_context_for_logging,
    )

    request_id = req.get("id")
    template_id = req.get("template_id")

    async with get_db_session() as db:
        existing = await get_by_request_id(db, request_id)
        if existing is not None and existing.status == "ACTIVE" and existing.device_id is not None:
            logger.info(
                "Dynamic instance for request %s already ACTIVE (device %s); skipping",
                request_id,
                existing.device_id,
            )
            return str(existing.device_id)

    template, hypervisor, secret = await _fetch_recipe_deps(template_id, None, client)
    if template is None:
        raise PermanentEventError(f"template {template_id} not found for request {request_id}")
    if hypervisor is None:
        raise PermanentEventError(
            f"hypervisor for template {template_id} not found for request {request_id}"
        )
    if secret is None:
        raise PermanentEventError(f"hypervisor secret not found for request {request_id}")

    hypervisor_id = template.get("hypervisor_id")

    # Insert (or reuse) the CREATING ledger row before any hypervisor action, so
    # a NAK mid-create leaves a row to retry against.
    async with get_db_session() as db:
        row = await insert_or_get_creating(
            db, request_id, reservation_id, template_id, hypervisor_id
        )
        if row.status == "ACTIVE" and row.device_id is not None:
            return str(row.device_id)

    context, secret_keys = _build_recipe_context(
        template, hypervisor, secret, request_id, reservation_id, user_id
    )
    password_keys = extract_password_keys(template) | secret_keys
    redacted = redact_context_for_logging(context, password_keys)

    driver_id = uuid.UUID(template["driver_id"])
    driver_sha256 = template.get("driver_sha256") or "unknown"
    driver_filename = template.get("driver_filename") or "driver.zip"
    connection_type = template.get("connection_type") or "Hypervisor"

    # The recipe acts on the hypervisor; there is no device yet, so ExecutionRun
    # rows for the create are keyed on the hypervisor id.
    hypervisor_uuid = uuid.UUID(str(hypervisor_id))
    user_uuid = uuid.UUID(user_id)
    res_uuid = uuid.UUID(reservation_id)

    async with get_db_session() as db:
        try:
            driver_path = await load_driver(
                db, driver_id, driver_sha256, driver_filename, connection_type
            )
        except DriverPackageError as exc:
            # A structurally broken recipe (invalid archive, missing Driver class,
            # a missing required Hypervisor method, unparseable driver.py) can
            # never load, so NAK'ing through the full max_deliver ladder only
            # delays the DLQ. Dead-letter on first delivery with a diagnosable
            # reason (issue #279). This raises out of the per-instance loop, so a
            # broken package fails the whole provision_requested event, matching
            # the missing-config-resource permanent path above. A download failure
            # (inventory unreachable) stays a transient RuntimeError and is left
            # to propagate as a NAK.
            raise PermanentEventError(
                f"recipe package cannot load for request {request_id}: {exc}"
            ) from exc

        login = await _run_recipe_step(
            db,
            hypervisor_uuid,
            driver_id,
            driver_sha256,
            "login",
            user_uuid,
            redacted,
            res_uuid,
            driver_path,
            context,
            password_keys,
            dedupe_key=dedupe_key,
        )
        if not login["success"]:
            raise RuntimeError(
                f"recipe login failed for request {request_id}: {login.get('error')}"
            )

        create = await _run_recipe_step(
            db,
            hypervisor_uuid,
            driver_id,
            driver_sha256,
            "create_instance",
            user_uuid,
            redacted,
            res_uuid,
            driver_path,
            context,
            password_keys,
            dedupe_key=dedupe_key,
        )
        # Always log out so a failed create does not leak a hypervisor session.
        await _run_recipe_step(
            db,
            hypervisor_uuid,
            driver_id,
            driver_sha256,
            "logout",
            user_uuid,
            redacted,
            res_uuid,
            driver_path,
            context,
            password_keys,
            dedupe_key=dedupe_key,
        )

        if not _recipe_reported_success(create):
            raise RuntimeError(
                f"recipe create_instance did not succeed for request {request_id}: "
                f"{create.get('error')}"
            )

        output = create.get("output") or {}
        instance_ref = output.get("instance_ref")
        field_data = output.get("field_data") or {}

        # Persist the hypervisor-side handle immediately: if the device create
        # below fails and NAKs, teardown can still destroy the instance.
        await set_instance_ref(db, request_id, instance_ref)

    device = await _create_dynamic_device(
        client, template_id, reservation_id, field_data, request_id
    )
    if device is None:
        raise PermanentEventError(
            f"inventory rejected dynamic device create for request {request_id}"
        )
    device_id = device["id"]

    async with get_db_session() as db:
        await mark_active(db, request_id, device_id, instance_ref)

    logger.info(
        "Materialized dynamic instance for request %s as device %s",
        request_id,
        device_id,
    )
    return str(device_id)


async def _handle_provision_requested(
    event_data: dict, get_db_session, dedupe_key: str | None = None
) -> None:
    """Create every dynamic instance a reservation booked, then report success.

    Raises out to process_reservation_message on any failure (transient NAK,
    permanent DLQ); only when all requests are ACTIVE does it post the success
    callback. The success callback is retry-then-log, so a lost callback falls
    to the reservations timeout backstop rather than NAK'ing an already-done
    provisioning.
    """
    reservation_id = event_data.get("reservation_id")
    user_id = event_data.get("user_id")
    requests = event_data.get("dynamic_requests", [])

    if not reservation_id:
        logger.warning("provision_requested with no reservation_id; skipping")
        return

    logger.info(
        "Processing provision_requested",
        extra={"reservation_id": reservation_id, "request_count": len(requests)},
    )

    device_ids: list[str] = []
    async with httpx.AsyncClient() as client:
        for req in requests:
            device_id = await _provision_one_instance(
                req, reservation_id, user_id, get_db_session, dedupe_key, client
            )
            device_ids.append(device_id)

    # Reached only when every request is ACTIVE.
    await _post_provision_result_best_effort(
        reservation_id, succeeded=True, device_ids=device_ids, error=None
    )
    logger.info(
        "Completed provision_requested",
        extra={"reservation_id": reservation_id, "device_count": len(device_ids)},
    )


async def _teardown_one_instance(
    row,
    reservation_id: str,
    user_id: str,
    get_db_session,
    client,
) -> None:
    """Destroy one dynamic instance, mirroring the L3 teardown discipline.

    A CREATING row with no instance_ref has nothing hypervisor-side; mark it
    DESTROYED. Otherwise load the recipe and run login/destroy_instance/logout,
    then delete the materialized device (404 is success), then mark DESTROYED. A
    driver-result failure does NOT raise (ACK, row stays ACTIVE as an accurate
    may-still-exist record); a TransientUpstreamError raises and NAKs.
    """
    from app.services.driver_loader import load_driver
    from app.services.dynamic_instance_service import mark_destroyed
    from app.services.execution_service import (
        extract_password_keys,
        redact_context_for_logging,
    )

    request_id = row.request_id
    device_id = str(row.device_id) if row.device_id is not None else None

    # Nothing was created hypervisor-side: no instance_ref means create_instance
    # never landed. Delete any stray device and retire the row.
    if not row.instance_ref:
        if device_id is not None:
            await _delete_dynamic_device(client, device_id)
        async with get_db_session() as db:
            await mark_destroyed(db, request_id)
        return

    template, hypervisor, secret = await _fetch_recipe_deps(
        str(row.template_id), str(row.hypervisor_id), client
    )
    if template is None or hypervisor is None or secret is None:
        # The recipe's config is gone (404), so we cannot drive destroy_instance.
        # Leave the row ACTIVE as a may-still-exist record and ACK (retrying will
        # not resurrect a deleted template); do not raise.
        logger.warning(
            "Cannot load recipe deps to destroy instance for request %s; leaving "
            "ledger row ACTIVE as a may-still-exist record",
            request_id,
        )
        return

    context, secret_keys = _build_recipe_context(
        template, hypervisor, secret, request_id, reservation_id, user_id
    )
    password_keys = extract_password_keys(template) | secret_keys
    redacted = redact_context_for_logging(context, password_keys)

    driver_id = uuid.UUID(template["driver_id"])
    driver_sha256 = template.get("driver_sha256") or "unknown"
    driver_filename = template.get("driver_filename") or "driver.zip"
    connection_type = template.get("connection_type") or "Hypervisor"

    hypervisor_uuid = uuid.UUID(str(row.hypervisor_id))
    user_uuid = uuid.UUID(user_id)
    res_uuid = uuid.UUID(reservation_id)

    async with get_db_session() as db:
        try:
            driver_path = await load_driver(
                db, driver_id, driver_sha256, driver_filename, connection_type
            )
        except Exception as e:
            logger.error(
                "Failed to load recipe to destroy request %s: %s; leaving ACTIVE",
                request_id,
                e,
            )
            return

        login = await _run_recipe_step(
            db,
            hypervisor_uuid,
            driver_id,
            driver_sha256,
            "login",
            user_uuid,
            redacted,
            res_uuid,
            driver_path,
            context,
            password_keys,
        )
        if not login["success"]:
            logger.error(
                "Recipe login failed during teardown of request %s; leaving ACTIVE",
                request_id,
            )
            return

        destroy = await _run_recipe_step(
            db,
            hypervisor_uuid,
            driver_id,
            driver_sha256,
            "destroy_instance",
            user_uuid,
            redacted,
            res_uuid,
            driver_path,
            context,
            password_keys,
            method_kwargs={"instance_ref": row.instance_ref},
        )
        await _run_recipe_step(
            db,
            hypervisor_uuid,
            driver_id,
            driver_sha256,
            "logout",
            user_uuid,
            redacted,
            res_uuid,
            driver_path,
            context,
            password_keys,
        )

    if not _recipe_reported_success(destroy):
        # Driver-result failure (the L3 discipline): ACK, leave the row ACTIVE as
        # an accurate "instance may still exist" record, and log it.
        logger.error(
            "destroy_instance did not cleanly succeed for request %s; leaving "
            "ledger row ACTIVE (instance may still exist)",
            request_id,
        )
        return

    # Instance is gone hypervisor-side; retire its device (404 = already gone).
    if device_id is not None:
        deleted = await _delete_dynamic_device(client, device_id)
        if not deleted:
            logger.error(
                "Device delete failed for request %s after destroy_instance; "
                "leaving ledger row ACTIVE",
                request_id,
            )
            return

    async with get_db_session() as db:
        await mark_destroyed(db, request_id)
    logger.info("Destroyed dynamic instance for request %s", request_id)


async def _execute_dynamic_teardown(
    reservation_id: str | None,
    user_id: str,
    get_db_session,
    client,
    removed_device_ids: list[str] | None = None,
) -> None:
    """Tear down a reservation's dynamic instances from the ledger (issue #32).

    When removed_device_ids is None every CREATING/ACTIVE row of the reservation
    is torn down (complete, cancel, fail); when it is a list only rows whose
    materialized device is in it are (the reservation.updated removal path).
    Already-DESTROYED rows are excluded by list_teardown_candidates, so a
    redelivery is a no-op for them.
    """
    if not reservation_id:
        return

    from app.services.dynamic_instance_service import list_teardown_candidates

    async with get_db_session() as db:
        candidates = await list_teardown_candidates(db, reservation_id)

    if removed_device_ids is not None:
        removed = {str(d) for d in removed_device_ids}
        candidates = [
            c for c in candidates if c.device_id is not None and str(c.device_id) in removed
        ]
    if not candidates:
        return

    for row in candidates:
        await _teardown_one_instance(row, reservation_id, user_id, get_db_session, client)


# --- Connection-driven L1 reconcile (ADR 0007, issue #345 P3b phase 3) --------
#
# A reservation.wiring_changed event carries a fork-save's released/built L1 hop
# delta (or, for a sweeper heal, no delta at all). The consumer applies it to
# hardware connection-by-connection: disconnect the released switch cross-connects,
# then connect the built ones, keyed by the l1_connection_assignments table (phase 1).
# Ordering, gap-reconcile, verbatim-hop apply, per-connection failure, and the frozen
# no-op guard are all Decision 4 to 7 of ADR 0007.

# System actor for consumer-driven execution runs: wiring_changed carries no acting
# user (it is a reconcile, not a user action), so runs are attributed to the nil UUID.
WIRING_SYSTEM_USER = uuid.UUID(int=0)


def _chain_walk_group(
    group: list[dict],
    device_is_switch: dict[str, bool],
) -> tuple[dict[str, list[tuple[str, str, str | None]]], list[tuple[dict, str]]]:
    """Chain-walk one hop set into switch cross-connect pairs, order-independently.

    `group` is a list of resolved hop dicts (ep_a, ep_b, phys, wire). It is either the
    hops of one canvas edge (grouped by edge_key, a single path by construction) or the
    ungrouped NULL-edge_key remainder. Each (device, port) endpoint appears in at most
    one hop, so the set is disjoint simple chains. Build a device-level adjacency, split
    into connected components, and walk each from a degree-1 end: every interior L1
    switch was entered on one port and left on another, and those two ports are exactly
    its cross-connect for that path. A component that is not a simple chain (a switch
    touched by more than two hops, or a cycle) is ambiguous once flattened, so every hop
    in it lands unresolvable rather than a guessed pairing. Within an edge_key group this
    never happens (one edge is one simple path); it is the fail-safe for the NULL set.

    Returns (pairs_by_switch, unresolvable) for this group.
    """
    touches: dict[str, list[tuple[str, int]]] = {}
    for idx, rw in enumerate(group):
        da, pa = rw["ep_a"]
        db_dev, pb = rw["ep_b"]
        touches.setdefault(da, []).append((pa, idx))
        touches.setdefault(db_dev, []).append((pb, idx))

    def _other(idx: int, dev: str, port: str) -> tuple[str, str]:
        rw = group[idx]
        return rw["ep_b"] if rw["ep_a"] == (dev, port) else rw["ep_a"]

    pairs_by_switch: dict[str, list[tuple[str, str, str | None]]] = {}
    unresolvable: list[tuple[dict, str]] = []
    seen_dev: set[str] = set()
    for start_dev in list(touches.keys()):
        if start_dev in seen_dev:
            continue
        # BFS the connected component and collect its hop indices.
        comp_devices: list[str] = []
        comp_hops: set[int] = set()
        stack = [start_dev]
        seen_dev.add(start_dev)
        while stack:
            d = stack.pop()
            comp_devices.append(d)
            for port, idx in touches[d]:
                comp_hops.add(idx)
                nd, _np = _other(idx, d, port)
                if nd not in seen_dev:
                    seen_dev.add(nd)
                    stack.append(nd)

        degrees = {d: len(touches[d]) for d in comp_devices}
        endpoints = [d for d in comp_devices if degrees[d] == 1]
        # A simple chain: no node above degree 2, and two degree-1 ends (a lone hop is
        # two ends). Zero ends is a cycle; a degree above 2 is a branch. Either is
        # ambiguous, so fail every hop in the component rather than mis-pair.
        if max(degrees.values()) > 2 or len(endpoints) != 2:
            for idx in comp_hops:
                unresolvable.append((group[idx]["wire"], WIRING_NOT_SIMPLE_CHAIN_REASON))
            continue

        # Walk the path from one end. Track the port the walk arrived on; an interior
        # switch's (arrival_port, departure_port) is its cross-connect for this path.
        current = endpoints[0]
        prev_hop = -1
        in_port: str | None = None
        while True:
            out_entry = next(((p, i) for p, i in touches[current] if i != prev_hop), None)
            if out_entry is None:
                break  # reached the far end of the chain
            out_port, idx = out_entry
            if in_port is not None and device_is_switch.get(current):
                pairs_by_switch.setdefault(current, []).append(
                    (in_port, out_port, group[idx]["phys"])
                )
            nd, np_ = _other(idx, current, out_port)
            prev_hop = idx
            in_port = np_
            current = nd
    return pairs_by_switch, unresolvable


async def _wires_to_switch_pairs(
    wires: list[dict],
    ctx: "_FetchContext",
) -> tuple[dict[str, list[tuple[str, str, str | None]]], list[tuple[dict, str]]]:
    """Derive L1 switch cross-connect pairs from recorded wires, grouped by edge_key.

    Each recorded wire is one physical hop between the port-endpoints (device_a, port_a)
    and (device_b, port_b), applied verbatim (ADR 0007 Decision 5). A canvas edge that
    routes through one or more L1 matrix switches is recorded as a run of such hops:
    A to SW1 to SW2 to B is three hops. The cross-connect a switch must apply is the
    pair of ports by which its path enters and leaves it, and that pairing lives in the
    edge, NOT in any single hop: SW1's two ports sit in two different hop rows.

    Pairing is BY edge_key FIRST (issue #345, PR #367 propagates the canvas edge id each
    hop was resolved from through cabling's fork GET, the save-delta, and this event).
    Grouping by edge_key makes the pairing exact: within one group the hops are one path
    by construction, so the chain-walk is unambiguous even when two groups traverse the
    SAME switch (each group's chain yields that switch's own cross-connect). A NULL/absent
    edge_key is a pre-migration (legacy) row with no grouping; the whole NULL remainder is
    chain-walked as one set with the not-a-simple-chain fail-safe, so a genuinely
    ambiguous legacy delta (two edges on one switch, no keys) fails rather than mis-pairs.
    A mixed delta processes keyed groups exactly and the NULL remainder via the fallback.

    Positional pairing (group a switch's hop ports, pair them by array order) is UNSOUND
    and NOT used: the delta arrays carry no reliable order. cabling builds released/built
    from set differences (fork_save_service reconcile_connection_sets), and the
    full-reconcile source orders fork_connections by a shared-timestamp created_at, so
    equal-time ties are unstable. Array-order pairing can physically mis-cross-connect.

    Returns (pairs_by_switch, unresolvable):
      pairs_by_switch: switch_id -> list of (port_a, port_b, physical_connection_id).
                       A switch may carry several pairs from several edge_key groups.
      unresolvable:    list of (wire, reason). A hop whose endpoint device no longer
                       resolves is WIRING_UNRESOLVABLE_REASON (Decision 5); an ambiguous
                       (non-simple-chain) group is WIRING_NOT_SIMPLE_CHAIN_REASON.

    Raises TransientUpstreamError (through ctx.get_device) on a 5xx or transport error
    while classifying an endpoint, so an UPSTREAM outage NAKs the whole message rather
    than mis-classifying a live switch as gone (Decision 7 error split).
    """
    device_is_switch: dict[str, bool] = {}
    keyed_groups: dict[str, list[dict]] = {}
    null_group: list[dict] = []
    unresolvable: list[tuple[dict, str]] = []
    for wire in wires:
        da = str(wire.get("device_a_id"))
        pa = wire.get("port_a")
        db_dev = str(wire.get("device_b_id"))
        pb = wire.get("port_b")
        dev_a = await ctx.get_device(da)
        dev_b = await ctx.get_device(db_dev)
        if dev_a is None or dev_b is None:
            # A recorded hop endpoint is gone: verbatim apply cannot proceed and a
            # re-route is forbidden (Decision 5).
            unresolvable.append((wire, WIRING_UNRESOLVABLE_REASON))
            continue
        if da == db_dev:
            # A self-loop hop is not a simple chain link; refuse to guess.
            unresolvable.append((wire, WIRING_NOT_SIMPLE_CHAIN_REASON))
            continue
        device_is_switch[da] = dev_a.get("connection_type") == "Layer 1 Switch"
        device_is_switch[db_dev] = dev_b.get("connection_type") == "Layer 1 Switch"
        rw = {
            "ep_a": (da, pa),
            "ep_b": (db_dev, pb),
            "phys": wire.get("physical_connection_id"),
            "wire": wire,
        }
        edge_key = wire.get("edge_key")
        if edge_key is None:
            null_group.append(rw)
        else:
            keyed_groups.setdefault(str(edge_key), []).append(rw)

    pairs_by_switch: dict[str, list[tuple[str, str, str | None]]] = {}
    groups = list(keyed_groups.values())
    if null_group:
        groups.append(null_group)
    for group in groups:
        group_pairs, group_unresolvable = _chain_walk_group(group, device_is_switch)
        for switch_id, pairs in group_pairs.items():
            pairs_by_switch.setdefault(switch_id, []).extend(pairs)
        unresolvable.extend(group_unresolvable)
    return pairs_by_switch, unresolvable


async def _run_driver_with_retry(
    driver_path: str,
    action: str,
    context: dict,
    password_keys: set,
    method_kwargs: dict | None = None,
) -> tuple[bool, int, str | None, dict | None]:
    """Run one driver action in the sandbox with bounded in-line retry/backoff.

    Mirrors run_driver_action's discipline (ADR 0007 Decision 6 item 1): a transient
    driver failure (a sandbox result with success false, or a raised exception) is
    retried up to WIRING_DRIVER_ATTEMPTS with exponential backoff. Returns
    (success, attempts, last_error, last_result). A driver failure is compensated
    per connection and never NAKs the message (Decision 7).
    """
    delay = WIRING_DRIVER_INITIAL_DELAY
    last_error: str | None = None
    last_result: dict | None = None
    attempt = 0
    for attempt in range(1, WIRING_DRIVER_ATTEMPTS + 1):
        try:
            result = await _run_sandbox(
                driver_path,
                action,
                context,
                method_kwargs=method_kwargs,
                password_keys=password_keys,
            )
        except Exception as exc:  # noqa: BLE001 - sandbox failures are per-connection
            last_error = f"driver raised: {exc}"
            last_result = None
        else:
            last_result = result
            # Gate on the driver RESULT payload, not only the transport-level sandbox
            # success (#345 phase 4 live-gate): a driver returning success=False
            # without raising is a per-connection failure to retry/park, not a success.
            op_failed, op_error = driver_result_failed(result)
            if not op_failed:
                return True, attempt, None, result
            last_error = op_error
        if attempt < WIRING_DRIVER_ATTEMPTS:
            await asyncio.sleep(min(delay, WIRING_DRIVER_MAX_DELAY))
            delay *= WIRING_DRIVER_BACKOFF_FACTOR
    return False, attempt, last_error, last_result


async def _apply_wiring_pairs(
    reservation_id: str,
    release_by_switch: dict[str, list[tuple[str, str, str | None]]],
    build_by_switch: dict[str, list[tuple[str, str, str | None]]],
    unresolvable: list[tuple[dict, str, str]],
    ctx: "_FetchContext",
    get_db_session,
) -> None:
    """Apply an L1 reconcile: release the released pairs, then build the built ones.

    Release-before-build in one pass, per switch (ADR 0007 Decision 4): a moved cable
    frees its old port before the new claim, so the ACTIVE-only partial-unique index is
    never tripped. Each connection is independent (Decision 6): a driver failure after
    the in-line retry cap lands a FAILED row and the pass continues; it never aborts the
    surviving connections and never NAKs. Assignment rows flip exactly as phase 1's
    projection does: record_l1_connect on a successful connect, release_l1_connection on
    a successful disconnect. A recorded hop that no longer resolves to a live
    switch/port is a FAILED row with the pinned unresolvable reason (Decision 5).

    `unresolvable` items carry the direction (ADR 0009 Decision 2, issue #369) the
    caller resolved them from: (wire, reason, intended), so a hop that fell out of
    the RELEASE side of a delta parks FAILED with intended RELEASED, not the
    build-direction default.
    """
    from datetime import datetime, timezone

    from app.services.driver_loader import load_driver
    from app.services.execution_service import (
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.l1_assignment_service import (
        is_pair_active,
        pair_needs_release,
        record_l1_failed,
    )

    res_uuid = uuid.UUID(reservation_id)

    # Unresolvable hops (Decision 5): a recorded endpoint is gone, or the hop set is
    # not a simple chain so its pairing is unrecoverable. A verbatim apply cannot
    # proceed and re-routing/guessing is forbidden. Park each hop FAILED with its
    # pinned reason, keyed on the hop's own recorded endpoints.
    for wire, reason, wire_intended in unresolvable:
        async with get_db_session() as db:
            await record_l1_failed(
                db,
                res_uuid,
                str(wire.get("device_a_id")),
                str(wire.get("port_a")),
                str(wire.get("port_b")),
                attempts=0,
                last_error=reason,
                physical_connection_id=wire.get("physical_connection_id"),
                intended=wire_intended,
            )

    switch_ids = list({*release_by_switch.keys(), *build_by_switch.keys()})
    for switch_id in switch_ids:
        release_pairs = release_by_switch.get(switch_id, [])
        build_pairs = build_by_switch.get(switch_id, [])
        switch_uuid = uuid.UUID(switch_id)

        switch_data = await ctx.get_device(switch_id)
        template_data = (
            await _fetch_template(switch_data.get("template_id", ""), ctx.client)
            if switch_data
            else None
        )
        driver_path = None
        load_error: str | None = None
        if switch_data is None:
            load_error = f"{WIRING_UNRESOLVABLE_REASON}: switch {switch_id} not found"
        elif template_data is None:
            load_error = f"{WIRING_UNRESOLVABLE_REASON}: template for switch {switch_id} not found"

        context: dict = {}
        password_keys: set = set()
        driver_id = None
        driver_sha256 = "unknown"
        if load_error is None:
            driver_id = uuid.UUID(switch_data["driver_id"])
            driver_sha256 = switch_data.get("driver_sha256", "unknown")
            driver_filename = switch_data.get("driver_filename", "driver.zip")
            connection_type = switch_data.get("connection_type", "Layer 1 Switch")
            context = build_context(switch_data, switch_uuid, WIRING_SYSTEM_USER, res_uuid)
            password_keys = extract_password_keys(template_data)
            async with get_db_session() as db:
                try:
                    driver_path = await load_driver(
                        db, driver_id, driver_sha256, driver_filename, connection_type
                    )
                except Exception as exc:  # noqa: BLE001 - a load failure strands the hops
                    load_error = f"{WIRING_UNRESOLVABLE_REASON}: driver load failed: {exc}"

        # A switch we cannot drive: every pair on it (release and build) is a FAILED
        # unresolvable row. A release with nothing believed live is still a no-op
        # (nothing to tear down), so only build/held pairs are unconditionally
        # surfaced as failures.
        if load_error is not None or driver_path is None:
            async with get_db_session() as db:
                for port_a, port_b, phys in build_pairs:
                    await record_l1_failed(
                        db,
                        res_uuid,
                        switch_uuid,
                        port_a,
                        port_b,
                        0,
                        load_error,
                        phys,
                        intended="ACTIVE",
                    )
                for port_a, port_b, phys in release_pairs:
                    if await pair_needs_release(db, res_uuid, switch_uuid, port_a, port_b):
                        await record_l1_failed(
                            db,
                            res_uuid,
                            switch_uuid,
                            port_a,
                            port_b,
                            0,
                            load_error,
                            phys,
                            intended="RELEASED",
                        )
            continue

        redacted = redact_context_for_logging(context, password_keys)

        async with get_db_session() as db:
            # Login once per switch.
            login_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "login",
                WIRING_SYSTEM_USER,
                redacted,
                res_uuid,
            )
            login_started = datetime.now(timezone.utc)
            login_ok, login_attempts, login_err, login_result = await _run_driver_with_retry(
                driver_path, "login", context, password_keys
            )
            await update_execution_run(
                db,
                login_run,
                "SUCCESS" if login_ok else "FAILED",
                output=(
                    json.dumps(login_result["output"], default=str)
                    if login_ok and login_result
                    else None
                ),
                error=None if login_ok else login_err,
                started_at=login_started,
                completed_at=datetime.now(timezone.utc),
            )
            if not login_ok:
                # A driver-level login failure (Decision 7: never NAKs). Every pair we
                # would have applied on this switch is parked FAILED with the attempts,
                # tagged with the direction it was going (issue #369).
                for port_a, port_b, phys in build_pairs:
                    await record_l1_failed(
                        db,
                        res_uuid,
                        switch_uuid,
                        port_a,
                        port_b,
                        login_attempts,
                        f"driver login failed: {login_err}",
                        phys,
                        intended="ACTIVE",
                    )
                for port_a, port_b, phys in release_pairs:
                    if await pair_needs_release(db, res_uuid, switch_uuid, port_a, port_b):
                        await record_l1_failed(
                            db,
                            res_uuid,
                            switch_uuid,
                            port_a,
                            port_b,
                            login_attempts,
                            f"driver login failed: {login_err}",
                            phys,
                            intended="RELEASED",
                        )
                continue

            # Release-before-build. Disconnect the released pairs first so a moved
            # cable frees its port before the rebuild claims it.
            for port_a, port_b, phys in release_pairs:
                if not await pair_needs_release(db, res_uuid, switch_uuid, port_a, port_b):
                    # Nothing believed live for this pair (already released, never
                    # applied, or a FAILED row whose intended is ACTIVE): an
                    # idempotent no-op, no driver call and no hardware touch.
                    continue
                await _apply_one_port_action(
                    db,
                    "disconnect_ports",
                    driver_path,
                    context,
                    password_keys,
                    switch_uuid,
                    driver_id,
                    driver_sha256,
                    res_uuid,
                    port_a,
                    port_b,
                    phys,
                )

            # Build the built pairs.
            for port_a, port_b, phys in build_pairs:
                if await is_pair_active(db, res_uuid, switch_uuid, port_a, port_b):
                    # Convergent replay: the pair is already ACTIVE for this
                    # reservation, so the build is a no-op success, not a driver call.
                    continue
                await _apply_one_port_action(
                    db,
                    "connect_ports",
                    driver_path,
                    context,
                    password_keys,
                    switch_uuid,
                    driver_id,
                    driver_sha256,
                    res_uuid,
                    port_a,
                    port_b,
                    phys,
                )

            # Logout once per switch.
            logout_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "logout",
                WIRING_SYSTEM_USER,
                redacted,
                res_uuid,
            )
            logout_started = datetime.now(timezone.utc)
            logout_ok, _la, logout_err, logout_result = await _run_driver_with_retry(
                driver_path, "logout", context, password_keys
            )
            await update_execution_run(
                db,
                logout_run,
                "SUCCESS" if logout_ok else "FAILED",
                output=(
                    json.dumps(logout_result["output"], default=str)
                    if logout_ok and logout_result and logout_result.get("output")
                    else None
                ),
                error=None if logout_ok else logout_err,
                started_at=logout_started,
                completed_at=datetime.now(timezone.utc),
            )


async def _apply_one_port_action(
    db,
    action: str,
    driver_path: str,
    context: dict,
    password_keys: set,
    switch_uuid: uuid.UUID,
    driver_id: uuid.UUID,
    driver_sha256: str,
    res_uuid: uuid.UUID,
    port_a: str,
    port_b: str,
    phys: str | None,
) -> None:
    """Apply one connect/disconnect cross-connect, flipping its assignment row.

    Writes an execution_run audit row, runs the driver with bounded retry, and on
    success flips the l1_connection_assignments projection (ACTIVE on connect,
    RELEASED on disconnect); on exhausting the retry cap it lands a FAILED row with the
    attempts and last_error, leaving siblings untouched (ADR 0007 Decision 6).
    """
    from datetime import datetime, timezone

    from app.services.execution_service import (
        create_execution_run,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.l1_assignment_service import (
        record_l1_connect,
        record_l1_failed,
        release_l1_connection,
    )

    redacted = redact_context_for_logging(context, password_keys)
    port_kwargs = {"port_a": port_a, "port_b": port_b}
    run = await create_execution_run(
        db,
        switch_uuid,
        driver_id,
        driver_sha256,
        action,
        WIRING_SYSTEM_USER,
        redacted,
        res_uuid,
        port_a,
        port_b,
        method_kwargs=port_kwargs,
    )
    started = datetime.now(timezone.utc)
    ok, attempts, err, result = await _run_driver_with_retry(
        driver_path, action, context, password_keys, method_kwargs=port_kwargs
    )
    if ok:
        await update_execution_run(
            db,
            run,
            "SUCCESS",
            output=json.dumps(result["output"], default=str) if result else None,
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )
        if action == "connect_ports":
            await record_l1_connect(db, res_uuid, switch_uuid, port_a, port_b, phys)
        else:
            await release_l1_connection(db, res_uuid, switch_uuid, port_a, port_b)
    else:
        await update_execution_run(
            db,
            run,
            "FAILED",
            error=err,
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )
        await record_l1_failed(
            db,
            res_uuid,
            switch_uuid,
            port_a,
            port_b,
            attempts,
            err,
            phys,
            intended="ACTIVE" if action == "connect_ports" else "RELEASED",
        )


# --- L2 layered reconcile (ADR 0009 phase 4, issue #416) ---------------------
#
# Membership is derived from the SAME recorded hops the L1 reconcile applies (option C,
# the #416 phase 4 resolution): a hop endpoint landing on a Layer 2 Switch implies that
# (switch, port) joins the reservation's VLAN on that switch's fabric, UNLESS the hop's
# other endpoint is ALSO a Layer 2 Switch, in which case the hop is an inter-switch trunk
# (assumed provisioned, no membership; membership is path-terminal only). fork_connections
# stay L1-hop-only; nothing new is recorded at save time. Unlike the legacy device-set
# resolver (_resolve_l2_switch_operations), which only saw a RESERVED device's DIRECT
# adjacency to an L2 switch, this hop walk also credits an L2 membership reached THROUGH
# intervening L1 matrix switches, since it classifies both endpoints of every recorded hop
# rather than only reserved-device adjacencies. That is the one deliberate divergence, and
# it is strictly more correct (it never misses a terminal L2 port a multi-hop path reaches).


async def _derive_l2_memberships(
    wires: list[dict],
    ctx: "_FetchContext",
) -> set[tuple[str, str]]:
    """Derive the intended per-port L2 memberships from a set of recorded hops.

    Returns {(switch_device_id, switch_port)} for every hop endpoint that lands on a
    Layer 2 Switch whose opposite endpoint is not itself a Layer 2 Switch. Classifies
    each endpoint device through ctx.get_device (memoized per event), so a 5xx while
    classifying raises TransientUpstreamError and NAKs the whole message (Decision 7):
    an upstream outage must never be mistaken for "this port is not an L2 member".
    """
    memberships: set[tuple[str, str]] = set()
    for wire in wires:
        da = str(wire.get("device_a_id"))
        pa = wire.get("port_a")
        db_dev = str(wire.get("device_b_id"))
        pb = wire.get("port_b")
        dev_a = await ctx.get_device(da)
        dev_b = await ctx.get_device(db_dev)
        if dev_a is None or dev_b is None:
            # A recorded endpoint is gone: the hop is unresolvable, so it can imply no
            # membership. The L1 reconcile parks it FAILED; L2 simply omits it.
            continue
        a_is_l2 = dev_a.get("connection_type") == "Layer 2 Switch"
        b_is_l2 = dev_b.get("connection_type") == "Layer 2 Switch"
        if a_is_l2 and b_is_l2:
            # Inter-switch trunk: assumed provisioned, contributes no membership.
            continue
        if a_is_l2 and pa is not None:
            memberships.add((da, str(pa)))
        if b_is_l2 and pb is not None:
            memberships.add((db_dev, str(pb)))
    return memberships


async def _resolve_add_allocations(
    reservation_id: str,
    add_switch_ports: set[tuple[str, str]],
    get_db_session,
) -> dict[str, tuple[uuid.UUID, int]]:
    """Resolve (vlan_assignment_id, vlan_id) per switch for the fabrics gaining a member.

    Groups the switches that have at least one ADD by fabric, allocates a conflict-free
    VLAN number per fabric via the unchanged find_or_assign_vlan (idempotent: an existing
    ACTIVE allocation for the (reservation, fabric) is returned, so a fabric with a live
    membership keeps its number), and reads back the vlan_assignments row id. Allocation
    happens on a fabric's FIRST built membership (ADR 0009 Decision 4): only switches with
    an add are grouped, so a fabric with no add allocates nothing here.
    """
    from app.models.vlan_assignment import VlanAssignment
    from app.services.vlan_service import fetch_fabric_id, find_or_assign_vlan

    switch_ids = {sid for sid, _port in add_switch_ports}
    switch_fabric: dict[str, uuid.UUID] = {}
    for sid in switch_ids:
        fid = await fetch_fabric_id(sid)
        if fid is None:
            fid = uuid.uuid5(uuid.NAMESPACE_DNS, sid)
            logger.warning("Could not determine fabric for L2 switch %s, using fallback", sid)
        switch_fabric[sid] = fid

    fabric_switches: dict[uuid.UUID, list[str]] = {}
    for sid, fid in switch_fabric.items():
        fabric_switches.setdefault(fid, []).append(sid)

    fabric_alloc: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
    async with get_db_session() as db:
        for fid, sids in fabric_switches.items():
            vlan_id = await find_or_assign_vlan(db, reservation_id, fid, sids)
            row = (
                await db.execute(
                    select(VlanAssignment).where(
                        VlanAssignment.reservation_id == uuid.UUID(reservation_id),
                        VlanAssignment.fabric_id == fid,
                        VlanAssignment.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                # Should not happen (find_or_assign just committed an ACTIVE row), but be
                # defensive: without the row id we cannot key a membership, so skip.
                logger.error(
                    "No ACTIVE vlan_assignment after find_or_assign for fabric %s "
                    "reservation %s; skipping its adds",
                    fid,
                    reservation_id,
                )
                continue
            fabric_alloc[fid] = (row.id, vlan_id)

    return {sid: fabric_alloc[fid] for sid, fid in switch_fabric.items() if fid in fabric_alloc}


async def _vlan_ids_for(
    vlan_assignment_ids: set[uuid.UUID],
    get_db_session,
) -> dict[uuid.UUID, int]:
    """Map vlan_assignment_id -> vlan_id (for driving remove_from_vlan on a leave).

    A leave reads its VLAN number from the allocation its membership row already points
    at, ACTIVE or RELEASED alike (a RELEASED allocation still carries the number the
    hardware was configured with), so the driver call names the right VLAN.
    """
    from app.models.vlan_assignment import VlanAssignment

    if not vlan_assignment_ids:
        return {}
    async with get_db_session() as db:
        rows = (
            (
                await db.execute(
                    select(VlanAssignment).where(VlanAssignment.id.in_(vlan_assignment_ids))
                )
            )
            .scalars()
            .all()
        )
    return {row.id: row.vlan_id for row in rows}


async def _release_orphaned_allocations(
    vlan_assignment_ids: set[uuid.UUID],
    get_db_session,
) -> None:
    """Release each vlan_assignment left with zero ACTIVE memberships (Decision 4).

    Runs AFTER the whole membership pass (removes and adds both applied), so a port moved
    to a different port on the same fabric leaves the fabric with one member and its
    allocation intact; only a fabric whose last member truly left is freed.
    """
    from datetime import datetime, timezone

    from app.models.vlan_assignment import VlanAssignment
    from app.services.l2_membership_service import count_active_memberships_for_vlan

    for va_id in vlan_assignment_ids:
        async with get_db_session() as db:
            if await count_active_memberships_for_vlan(db, va_id) > 0:
                continue
            row = (
                await db.execute(
                    select(VlanAssignment).where(
                        VlanAssignment.id == va_id,
                        VlanAssignment.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            row.status = "RELEASED"
            row.released_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Released orphaned VLAN allocation %s (last membership left)", va_id)


async def _apply_l2_memberships(
    reservation_id: str,
    removes: list[dict],
    adds: list[dict],
    ctx: "_FetchContext",
    get_db_session,
) -> None:
    """Apply an L2 membership reconcile: leave the removed ports, then join the added ones.

    `removes`/`adds` are dicts with switch_device_id, port, vlan_assignment_id, vlan_id.
    Per switch: login once, drive remove_from_vlan for each removed membership still
    believed live (membership_needs_remove gate), then add_to_vlan for each added
    membership not already ACTIVE (is_membership_active gate), then logout. Every driver
    call is result-gated through _run_driver_with_retry (Decision 3). A success flips the
    ledger (release_l2_membership on a leave, record_l2_membership_active on a join); an
    exhausted retry lands a FAILED row tagged with its direction (issue #369) and the pass
    continues (Decision 6, never NAKs). A switch that cannot be driven parks its adds and
    still-live removes FAILED. After the whole pass, an allocation left with no ACTIVE
    membership is released (allocation lifecycle coupling, Decision 4). This one apply is
    shared by the reconcile and both retry channels.
    """
    from datetime import datetime, timezone

    from app.services.driver_loader import load_driver
    from app.services.execution_service import (
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.l2_membership_service import (
        is_membership_active,
        membership_needs_remove,
        record_l2_failed,
    )

    res_uuid = uuid.UUID(reservation_id)
    touched_allocations: set[uuid.UUID] = {uuid.UUID(str(r["vlan_assignment_id"])) for r in removes}

    removes_by_switch: dict[str, list[dict]] = {}
    adds_by_switch: dict[str, list[dict]] = {}
    for r in removes:
        removes_by_switch.setdefault(str(r["switch_device_id"]), []).append(r)
    for a in adds:
        adds_by_switch.setdefault(str(a["switch_device_id"]), []).append(a)

    switch_ids = list({*removes_by_switch.keys(), *adds_by_switch.keys()})
    for switch_id in switch_ids:
        switch_uuid = uuid.UUID(switch_id)
        switch_removes = removes_by_switch.get(switch_id, [])
        switch_adds = adds_by_switch.get(switch_id, [])

        switch_data = await ctx.get_device(switch_id)
        template_data = (
            await _fetch_template(switch_data.get("template_id", ""), ctx.client)
            if switch_data
            else None
        )
        load_error: str | None = None
        if switch_data is None:
            load_error = f"{WIRING_UNRESOLVABLE_REASON}: L2 switch {switch_id} not found"
        elif template_data is None:
            load_error = (
                f"{WIRING_UNRESOLVABLE_REASON}: template for L2 switch {switch_id} not found"
            )

        context: dict = {}
        password_keys: set = set()
        driver_id = None
        driver_sha256 = "unknown"
        driver_path = None
        if load_error is None:
            driver_id = uuid.UUID(switch_data["driver_id"])
            driver_sha256 = switch_data.get("driver_sha256", "unknown")
            driver_filename = switch_data.get("driver_filename", "driver.zip")
            connection_type = switch_data.get("connection_type", "Layer 2 Switch")
            context = build_context(switch_data, switch_uuid, WIRING_SYSTEM_USER, res_uuid)
            password_keys = extract_password_keys(template_data)
            async with get_db_session() as db:
                try:
                    driver_path = await load_driver(
                        db, driver_id, driver_sha256, driver_filename, connection_type
                    )
                except Exception as exc:  # noqa: BLE001 - a load failure strands the ops
                    load_error = f"{WIRING_UNRESOLVABLE_REASON}: driver load failed: {exc}"

        if load_error is not None or driver_path is None:
            async with get_db_session() as db:
                for a in switch_adds:
                    await record_l2_failed(
                        db,
                        res_uuid,
                        a["vlan_assignment_id"],
                        switch_uuid,
                        a["port"],
                        0,
                        load_error,
                        intended="ACTIVE",
                    )
                for r in switch_removes:
                    if await membership_needs_remove(db, res_uuid, switch_uuid, r["port"]):
                        await record_l2_failed(
                            db,
                            res_uuid,
                            r["vlan_assignment_id"],
                            switch_uuid,
                            r["port"],
                            0,
                            load_error,
                            intended="RELEASED",
                        )
            continue

        redacted = redact_context_for_logging(context, password_keys)
        async with get_db_session() as db:
            login_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "login",
                WIRING_SYSTEM_USER,
                redacted,
                res_uuid,
            )
            login_started = datetime.now(timezone.utc)
            login_ok, login_attempts, login_err, login_result = await _run_driver_with_retry(
                driver_path, "login", context, password_keys
            )
            await update_execution_run(
                db,
                login_run,
                "SUCCESS" if login_ok else "FAILED",
                output=(
                    json.dumps(login_result["output"], default=str)
                    if login_ok and login_result
                    else None
                ),
                error=None if login_ok else login_err,
                started_at=login_started,
                completed_at=datetime.now(timezone.utc),
            )
            if not login_ok:
                for a in switch_adds:
                    await record_l2_failed(
                        db,
                        res_uuid,
                        a["vlan_assignment_id"],
                        switch_uuid,
                        a["port"],
                        login_attempts,
                        f"driver login failed: {login_err}",
                        intended="ACTIVE",
                    )
                for r in switch_removes:
                    if await membership_needs_remove(db, res_uuid, switch_uuid, r["port"]):
                        await record_l2_failed(
                            db,
                            res_uuid,
                            r["vlan_assignment_id"],
                            switch_uuid,
                            r["port"],
                            login_attempts,
                            f"driver login failed: {login_err}",
                            intended="RELEASED",
                        )
                continue

            # Leave-before-join: remove departed memberships before adding new ones.
            for r in switch_removes:
                if not await membership_needs_remove(db, res_uuid, switch_uuid, r["port"]):
                    continue
                await _apply_one_vlan_action(
                    db,
                    "remove_from_vlan",
                    driver_path,
                    context,
                    password_keys,
                    switch_uuid,
                    driver_id,
                    driver_sha256,
                    res_uuid,
                    r["port"],
                    r["vlan_id"],
                    r["vlan_assignment_id"],
                )
            for a in switch_adds:
                if await is_membership_active(db, res_uuid, switch_uuid, a["port"]):
                    continue
                await _apply_one_vlan_action(
                    db,
                    "add_to_vlan",
                    driver_path,
                    context,
                    password_keys,
                    switch_uuid,
                    driver_id,
                    driver_sha256,
                    res_uuid,
                    a["port"],
                    a["vlan_id"],
                    a["vlan_assignment_id"],
                )

            logout_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "logout",
                WIRING_SYSTEM_USER,
                redacted,
                res_uuid,
            )
            logout_started = datetime.now(timezone.utc)
            logout_ok, _la, logout_err, logout_result = await _run_driver_with_retry(
                driver_path, "logout", context, password_keys
            )
            await update_execution_run(
                db,
                logout_run,
                "SUCCESS" if logout_ok else "FAILED",
                output=(
                    json.dumps(logout_result["output"], default=str)
                    if logout_ok and logout_result and logout_result.get("output")
                    else None
                ),
                error=None if logout_ok else logout_err,
                started_at=logout_started,
                completed_at=datetime.now(timezone.utc),
            )

    await _release_orphaned_allocations(touched_allocations, get_db_session)


async def _apply_one_vlan_action(
    db,
    action: str,
    driver_path: str,
    context: dict,
    password_keys: set,
    switch_uuid: uuid.UUID,
    driver_id: uuid.UUID,
    driver_sha256: str,
    res_uuid: uuid.UUID,
    port: str,
    vlan_id: int,
    vlan_assignment_id,
) -> None:
    """Apply one add_to_vlan/remove_from_vlan membership op, flipping its ledger row.

    Writes an execution_run audit row, runs the driver with bounded retry gated on the
    driver RESULT (Decision 3), and on success flips the l2_port_assignments projection
    (ACTIVE on add, RELEASED on remove); on exhausting the retry cap it lands a FAILED
    row tagged with the op's direction, leaving siblings untouched (Decision 6).
    """
    from datetime import datetime, timezone

    from app.services.execution_service import (
        create_execution_run,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.l2_membership_service import (
        record_l2_failed,
        record_l2_membership_active,
        release_l2_membership,
    )

    redacted = redact_context_for_logging(context, password_keys)
    method_kwargs = (
        {"port": port, "vlan_id": vlan_id, "tag": "tagged"}
        if action == "add_to_vlan"
        else {"port": port, "vlan_id": vlan_id}
    )
    run = await create_execution_run(
        db,
        switch_uuid,
        driver_id,
        driver_sha256,
        action,
        WIRING_SYSTEM_USER,
        redacted,
        res_uuid,
        port,
        method_kwargs=method_kwargs,
    )
    started = datetime.now(timezone.utc)
    ok, attempts, err, result = await _run_driver_with_retry(
        driver_path, action, context, password_keys, method_kwargs=method_kwargs
    )
    if ok:
        await update_execution_run(
            db,
            run,
            "SUCCESS",
            output=json.dumps(result["output"], default=str) if result else None,
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )
        if action == "add_to_vlan":
            await record_l2_membership_active(db, res_uuid, vlan_assignment_id, switch_uuid, port)
        else:
            await release_l2_membership(db, res_uuid, switch_uuid, port)
    else:
        await update_execution_run(
            db,
            run,
            "FAILED",
            error=err,
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )
        await record_l2_failed(
            db,
            res_uuid,
            vlan_assignment_id,
            switch_uuid,
            port,
            attempts,
            err,
            intended="ACTIVE" if action == "add_to_vlan" else "RELEASED",
        )


async def _reconcile_l2_memberships(
    reservation_id: str,
    intended_wires: list[dict],
    ctx: "_FetchContext",
    get_db_session,
) -> None:
    """Full L2 membership reconcile against cabling's intended wires (ADR 0009 phase 4).

    L2 membership is ALWAYS a full reconcile (not a per-hop delta), on every apply (delta,
    heal, or gap): two hops can imply the same (switch, port) membership, so a released
    hop alone cannot prove a membership should leave; only the intended set can. Derives
    the intended memberships (option C), diffs against this reservation's ACTIVE
    memberships, and drives removes-then-adds through the shared _apply_l2_memberships. The
    L1 reconcile keeps its verbatim delta semantics untouched; this runs after it so L2
    removes land after L1 builds (Decision 4 ordering).
    """
    from app.services.l2_membership_service import active_memberships_for_reservation

    intended = await _derive_l2_memberships(intended_wires, ctx)

    async with get_db_session() as db:
        active_rows = await active_memberships_for_reservation(db, reservation_id)
    current: dict[tuple[str, str], object] = {
        (str(row.switch_device_id), row.port): row for row in active_rows
    }

    add_keys = intended - set(current.keys())
    remove_keys = set(current.keys()) - intended

    if not add_keys and not remove_keys:
        return

    alloc_by_switch = await _resolve_add_allocations(reservation_id, add_keys, get_db_session)

    adds: list[dict] = []
    for switch_id, port in add_keys:
        alloc = alloc_by_switch.get(switch_id)
        if alloc is None:
            # No allocation resolved for this switch's fabric (upstream fabric lookup or
            # allocation failed); park the join FAILED so the retry channel revisits it.
            async with get_db_session() as db:
                from app.services.l2_membership_service import record_l2_failed

                await record_l2_failed(
                    db,
                    uuid.UUID(reservation_id),
                    None,
                    uuid.UUID(switch_id),
                    port,
                    0,
                    f"{WIRING_UNRESOLVABLE_REASON}: no VLAN allocation for fabric",
                    intended="ACTIVE",
                )
            continue
        va_id, vlan_id = alloc
        adds.append(
            {
                "switch_device_id": switch_id,
                "port": port,
                "vlan_assignment_id": va_id,
                "vlan_id": vlan_id,
            }
        )

    remove_alloc_ids = {current[key].vlan_assignment_id for key in remove_keys}
    vlan_by_alloc = await _vlan_ids_for(remove_alloc_ids, get_db_session)
    removes: list[dict] = []
    for switch_id, port in remove_keys:
        row = current[(switch_id, port)]
        removes.append(
            {
                "switch_device_id": switch_id,
                "port": port,
                "vlan_assignment_id": row.vlan_assignment_id,
                "vlan_id": vlan_by_alloc.get(row.vlan_assignment_id, 0),
            }
        )

    await _apply_l2_memberships(reservation_id, removes, adds, ctx, get_db_session)


# --- L3 layered reconcile (ADR 0009 phase 5, issue #416) ---------------------
#
# Adjacency is derived from the SAME recorded hops the L1 reconcile applies and the L2
# reconcile classifies (option C, layer-agnostic): a hop endpoint landing on a Layer 3
# Switch makes the reservation L3-adjacent to that switch, UNLESS the hop's other endpoint
# is ALSO a Layer 3 Switch, in which case the hop is an inter-switch trunk (the same trunk
# boundary L2 uses: it contributes no adjacency). Unlike the legacy device-set resolver
# (_resolve_l3_switch_operations), which only saw a RESERVED device's DIRECT adjacency to
# an L3 switch, this hop walk also credits an L3 switch reached THROUGH intervening L1
# matrix switches, since it classifies both endpoints of every recorded hop rather than
# only reserved-device adjacencies. That is the one deliberate divergence, and it is
# strictly more correct (it never misses an L3 switch a multi-hop path reaches).
#
# The pin lifecycle is UNCHANGED (issue #20): routes come from the switch's latest config
# version at first provision, pinned per reservation in route_assignments, captured once
# and reused verbatim by every later provision/deprovision/retry (get_effective_pinned_
# routes), NEVER re-derived. Phase 5 changes only WHEN a switch is provisioned or
# deprovisioned (it gained or lost adjacency in the intended set), and gates the pin's
# STATUS on the driver outcome. Adjacency is shared derived state (many hops can imply the
# same switch), so the L3 pass is ALWAYS a full reconcile against cabling's intended set,
# exactly like L2: a single released hop cannot prove adjacency ended.


async def _derive_l3_adjacency(
    wires: list[dict],
    ctx: "_FetchContext",
) -> set[str]:
    """Derive the intended L3-adjacent switch set from a set of recorded hops (option C).

    Returns {switch_device_id} for every hop endpoint that lands on a Layer 3 Switch whose
    opposite endpoint is not itself a Layer 3 Switch. Classifies each endpoint device
    through ctx.get_device (memoized per event), so a 5xx while classifying raises
    TransientUpstreamError and NAKs the whole message (Decision 7): an upstream outage must
    never be mistaken for "this switch is no longer adjacent". The L2 derivation's exact
    shape, keyed on the switch device id (an L3 pin is per switch, not per port).
    """
    switches: set[str] = set()
    for wire in wires:
        da = str(wire.get("device_a_id"))
        db_dev = str(wire.get("device_b_id"))
        dev_a = await ctx.get_device(da)
        dev_b = await ctx.get_device(db_dev)
        if dev_a is None or dev_b is None:
            # A recorded endpoint is gone: the hop is unresolvable, so it can imply no
            # adjacency. The L1 reconcile parks it FAILED; L3 simply omits it.
            continue
        a_is_l3 = dev_a.get("connection_type") == "Layer 3 Switch"
        b_is_l3 = dev_b.get("connection_type") == "Layer 3 Switch"
        if a_is_l3 and b_is_l3:
            # Inter-switch trunk: assumed provisioned, contributes no adjacency.
            continue
        if a_is_l3:
            switches.add(da)
        if b_is_l3:
            switches.add(db_dev)
    return switches


async def _apply_l3_adjacency(
    reservation_id: str,
    deprovisions: list[dict],
    provisions: list[dict],
    ctx: "_FetchContext",
    get_db_session,
) -> None:
    """Apply an L3 adjacency reconcile: deprovision departed switches, then provision new.

    `deprovisions`/`provisions` are dicts with device_id and routes (the pinned set).
    Switch sets are disjoint (adjacency is per-switch binary: a switch cannot both gain and
    lose adjacency in one reconcile), and deprovisions run first (ADR 0009 Decision 4
    ordering: L3 deprovision before L3 provision). Per switch: login once, drive
    remove_route (deprovision) or configure_route (provision) for each pinned route, then
    logout. Every driver call is result-gated through _run_driver_with_retry (Decision 3).
    A clean provision records the switch ACTIVE (record_route_active); a clean removal
    releases the pin (release_route_membership). A per-switch failure (any route failed, a
    login failure, or an undrivable switch) lands a FAILED row tagged with its direction
    (issue #369) and the pass continues (Decision 6, never NAKs). This one apply is shared
    by the reconcile and both retry channels, the _apply_l2_memberships analogue.
    """
    from datetime import datetime, timezone

    from app.services.driver_loader import load_driver
    from app.services.execution_service import (
        build_context,
        create_execution_run,
        extract_password_keys,
        redact_context_for_logging,
        update_execution_run,
    )
    from app.services.route_service import (
        record_route_active,
        record_route_failed,
        release_route_membership,
        route_needs_remove,
    )

    res_uuid = uuid.UUID(reservation_id)
    work = [("deprovision", d) for d in deprovisions] + [("provision", p) for p in provisions]

    for direction, item in work:
        switch_id = str(item["device_id"])
        routes = item["routes"] or []
        switch_uuid = uuid.UUID(switch_id)
        method = "remove_route" if direction == "deprovision" else "configure_route"
        intended = "RELEASED" if direction == "deprovision" else "ACTIVE"

        # A deprovision whose pin is no longer believed installed (already released, or a
        # FAILED-intended-ACTIVE row that never applied) is an idempotent no-op.
        if direction == "deprovision":
            async with get_db_session() as db:
                if not await route_needs_remove(db, res_uuid, switch_id):
                    continue

        switch_data = await ctx.get_device(switch_id)
        template_data = (
            await _fetch_template(switch_data.get("template_id", ""), ctx.client)
            if switch_data
            else None
        )
        load_error: str | None = None
        if switch_data is None:
            load_error = f"{WIRING_UNRESOLVABLE_REASON}: L3 switch {switch_id} not found"
        elif template_data is None:
            load_error = (
                f"{WIRING_UNRESOLVABLE_REASON}: template for L3 switch {switch_id} not found"
            )

        context: dict = {}
        password_keys: set = set()
        driver_id = None
        driver_sha256 = "unknown"
        driver_path = None
        if load_error is None:
            driver_id = uuid.UUID(switch_data["driver_id"])
            driver_sha256 = switch_data.get("driver_sha256", "unknown")
            driver_filename = switch_data.get("driver_filename", "driver.zip")
            connection_type = switch_data.get("connection_type", "Layer 3 Switch")
            context = build_context(switch_data, switch_uuid, WIRING_SYSTEM_USER, res_uuid)
            password_keys = extract_password_keys(template_data)
            async with get_db_session() as db:
                try:
                    driver_path = await load_driver(
                        db, driver_id, driver_sha256, driver_filename, connection_type
                    )
                except Exception as exc:  # noqa: BLE001 - a load failure strands the switch
                    load_error = f"{WIRING_UNRESOLVABLE_REASON}: driver load failed: {exc}"

        if load_error is not None or driver_path is None:
            async with get_db_session() as db:
                await record_route_failed(
                    db, res_uuid, switch_id, routes, 0, load_error, intended=intended
                )
            continue

        redacted = redact_context_for_logging(context, password_keys)
        async with get_db_session() as db:
            login_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "login",
                WIRING_SYSTEM_USER,
                redacted,
                res_uuid,
            )
            login_started = datetime.now(timezone.utc)
            login_ok, login_attempts, login_err, login_result = await _run_driver_with_retry(
                driver_path, "login", context, password_keys
            )
            await update_execution_run(
                db,
                login_run,
                "SUCCESS" if login_ok else "FAILED",
                output=(
                    json.dumps(login_result["output"], default=str)
                    if login_ok and login_result
                    else None
                ),
                error=None if login_ok else login_err,
                started_at=login_started,
                completed_at=datetime.now(timezone.utc),
            )
            if not login_ok:
                await record_route_failed(
                    db,
                    res_uuid,
                    switch_id,
                    routes,
                    login_attempts,
                    f"driver login failed: {login_err}",
                    intended=intended,
                )
                continue

            switch_ok = True
            switch_attempts = 0
            switch_last_error: str | None = None
            for route in routes:
                destination = route.get("destination")
                next_hop = route.get("next_hop")
                interface = route.get("interface")
                ident_a, ident_b = _route_run_identity(destination, next_hop, interface)
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
                    WIRING_SYSTEM_USER,
                    redacted,
                    res_uuid,
                    ident_a,
                    ident_b,
                    method_kwargs=route_kwargs,
                )
                op_started = datetime.now(timezone.utc)
                ok, attempts, err, result = await _run_driver_with_retry(
                    driver_path, method, context, password_keys, method_kwargs=route_kwargs
                )
                if not ok:
                    switch_ok = False
                    switch_attempts += attempts
                    switch_last_error = err
                await update_execution_run(
                    db,
                    run,
                    "SUCCESS" if ok else "FAILED",
                    output=json.dumps(result["output"], default=str)
                    if ok and result and result.get("output")
                    else None,
                    error=None if ok else err,
                    started_at=op_started,
                    completed_at=datetime.now(timezone.utc),
                )

            logout_run = await create_execution_run(
                db,
                switch_uuid,
                driver_id,
                driver_sha256,
                "logout",
                WIRING_SYSTEM_USER,
                redacted,
                res_uuid,
            )
            logout_started = datetime.now(timezone.utc)
            logout_ok, _la, logout_err, logout_result = await _run_driver_with_retry(
                driver_path, "logout", context, password_keys
            )
            await update_execution_run(
                db,
                logout_run,
                "SUCCESS" if logout_ok else "FAILED",
                output=(
                    json.dumps(logout_result["output"], default=str)
                    if logout_ok and logout_result and logout_result.get("output")
                    else None
                ),
                error=None if logout_ok else logout_err,
                started_at=logout_started,
                completed_at=datetime.now(timezone.utc),
            )

            if switch_ok:
                if direction == "provision":
                    await record_route_active(db, res_uuid, switch_id, routes)
                else:
                    await release_route_membership(db, res_uuid, switch_id)
            else:
                await record_route_failed(
                    db,
                    res_uuid,
                    switch_id,
                    routes,
                    switch_attempts or 1,
                    switch_last_error,
                    intended=intended,
                )


async def _reconcile_l3_adjacency(
    reservation_id: str,
    intended_wires: list[dict],
    ctx: "_FetchContext",
    get_db_session,
) -> None:
    """Full L3 adjacency reconcile against cabling's intended wires (ADR 0009 phase 5).

    L3 adjacency is ALWAYS a full reconcile (not a per-hop delta), on every apply (delta,
    heal, or gap): many hops can imply the same switch's adjacency, so a released hop alone
    cannot prove adjacency ended; only the intended set can. Derives the intended adjacency
    (option C), diffs against this reservation's ACTIVE route pins, and drives
    deprovisions-then-provisions through the shared _apply_l3_adjacency. Runs AFTER the L2
    pass (Decision 4 ordering). The pinned routes for a provision come from an existing
    non-RELEASED pin if one survives (reused verbatim, issue #20) or the switch's latest
    config version on a genuinely fresh provision; a switch whose config declares no routes
    is skipped and nothing is pinned.
    """
    from app.services.route_service import get_effective_pinned_routes, get_route_assignments

    intended = await _derive_l3_adjacency(intended_wires, ctx)

    async with get_db_session() as db:
        active_rows = await get_route_assignments(db, reservation_id)
    current: dict[str, object] = {str(row.device_id): row for row in active_rows}

    add_switches = intended - set(current.keys())
    remove_switches = set(current.keys()) - intended

    if not add_switches and not remove_switches:
        return

    provisions: list[dict] = []
    for switch_id in add_switches:
        async with get_db_session() as db:
            pinned = await get_effective_pinned_routes(db, reservation_id, switch_id)
        if pinned is None:
            detail = await ctx.get_latest_config(switch_id)
            pinned = ((detail or {}).get("config") or {}).get("routes") or []
        if not pinned:
            logger.info(
                "L3 switch %s has no routes in its latest config version; "
                "skipping adjacency provision for reservation %s",
                switch_id,
                reservation_id,
            )
            continue
        provisions.append({"device_id": switch_id, "routes": pinned})

    deprovisions: list[dict] = []
    for switch_id in remove_switches:
        row = current[switch_id]
        deprovisions.append({"device_id": switch_id, "routes": row.routes})

    if not provisions and not deprovisions:
        return

    await _apply_l3_adjacency(reservation_id, deprovisions, provisions, ctx, get_db_session)


async def handle_wiring_changed(
    event_data: dict, get_db_session, dedupe_key: str | None = None
) -> None:
    """Consume reservation.wiring_changed: ordered, connection-driven L1 reconcile.

    The ordering decision (ADR 0007 Decision 4), against
    reservation_wiring_state.last_applied_fork_version (LA):

      - frozen reservation:            no-op ack before any driver call (Decision 7).
      - fork_version <= LA:            stale/duplicate delivery, no-op ack.
      - delta-less event (heal), any:  full reconcile from cabling's intended set.
      - missing wiring_state row:      gap by definition, full reconcile, then stamp.
      - fork_version == LA + 1, delta: contiguous, apply the carried released/built.
      - fork_version >  LA + 1:        gap, full reconcile from cabling's intended set.

    The version is stamped after the pass even when it left FAILED rows (the version
    was processed). An UPSTREAM cabling/inventory failure raises TransientUpstreamError
    and NAKs; a per-connection DRIVER failure lands a FAILED row and acks (Decision 7).
    """
    from app.services.l1_assignment_service import get_wiring_state, stamp_last_applied

    reservation_id = event_data.get("reservation_id")
    fork_version = event_data.get("fork_version")
    released = event_data.get("released")
    built = event_data.get("built")

    if reservation_id is None or fork_version is None:
        logger.warning("wiring_changed event missing reservation_id or fork_version; ignoring")
        return

    # Frozen guard (Decision 7): a wiring_changed for an ended reservation is a no-op
    # before any driver call. Read once, up front.
    async with get_db_session() as db:
        state = await get_wiring_state(db, reservation_id)
        if state is not None and state.frozen:
            logger.info(
                "wiring_changed for frozen reservation %s (version %s); no-op",
                reservation_id,
                fork_version,
            )
            return
        last_applied = state.last_applied_fork_version if state is not None else None

    # Stale or duplicate: the version was already applied. No-op.
    if last_applied is not None and fork_version <= last_applied:
        logger.info(
            "wiring_changed version %s <= last_applied %s for reservation %s; stale no-op",
            fork_version,
            last_applied,
            reservation_id,
        )
        return

    delta_less = released is None or built is None
    # Full reconcile when: heal (no delta) at any version; a missing state row; a
    # version gap. Otherwise the contiguous carried-delta apply. The delta_less check
    # comes first so a heal at exactly last_applied + 1 takes the full-reconcile path
    # and applies the missed save, never the empty carried delta (ADR 0007 Decision 2).
    full_reconcile = delta_less or last_applied is None or fork_version != last_applied + 1

    async with httpx.AsyncClient() as client:
        ctx = _FetchContext(client)

        # L2 membership is ALWAYS a full reconcile against cabling's intended set (the
        # session lead's settled call, ADR 0009 phase 4): a released hop cannot prove a
        # membership should leave, only the intended set can. In the full_reconcile branch
        # the intended wires are fetched anyway (reused); the carried-delta branch fetches
        # them once here for the L2 pass. The fork stores L1-hop-only rows, so this is the
        # complete recorded-hop set L2 membership is derived from (option C).
        l2_intended_wires: list[dict]

        if full_reconcile:
            desired_wires = await _fetch_fork_intended_wires(str(reservation_id), client)
            l2_intended_wires = desired_wires
            desired_by_switch, unresolvable = await _wires_to_switch_pairs(desired_wires, ctx)

            # Convergent reconcile against the current ACTIVE rows: release ACTIVE pairs
            # not desired, build desired pairs not yet ACTIVE.
            async with get_db_session() as db:
                from app.services.l1_assignment_service import (
                    active_assignments_for_reservation,
                )
                from app.services.l1_assignment_service import (
                    canonical_port_pair as _canon,
                )

                active_rows = await active_assignments_for_reservation(db, reservation_id)
            current: dict[tuple[str, str, str], None] = {}
            for row in active_rows:
                ca, cb = _canon(row.port_a, row.port_b)
                current[(str(row.switch_device_id), ca, cb)] = None
            desired: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
            for switch_id, pairs in desired_by_switch.items():
                for port_a, port_b, phys in pairs:
                    ca, cb = _canon(port_a, port_b)
                    desired[(switch_id, ca, cb)] = (port_a, port_b, phys)

            release_by_switch: dict[str, list[tuple[str, str, str | None]]] = {}
            build_by_switch: dict[str, list[tuple[str, str, str | None]]] = {}
            for key in current.keys() - desired.keys():
                switch_id, ca, cb = key
                release_by_switch.setdefault(switch_id, []).append((ca, cb, None))
            for key in desired.keys() - current.keys():
                switch_id, ca, cb = key
                build_by_switch.setdefault(switch_id, []).append(desired[key])

            # unresolvable is resolved purely from the DESIRED (build) set, so every
            # item is build-direction (issue #369): intended ACTIVE.
            tagged_unresolvable = [(wire, reason, "ACTIVE") for wire, reason in unresolvable]
            await _apply_wiring_pairs(
                str(reservation_id),
                release_by_switch,
                build_by_switch,
                tagged_unresolvable,
                ctx,
                get_db_session,
            )
        else:
            l2_intended_wires = await _fetch_fork_intended_wires(str(reservation_id), client)
            release_by_switch, unresolvable_r = await _wires_to_switch_pairs(released, ctx)
            build_by_switch, unresolvable_b = await _wires_to_switch_pairs(built, ctx)
            # Tag each side with its direction (issue #369) before merging: a hop
            # that fell out of the RELEASE delta is intended RELEASED, one from the
            # BUILD delta is intended ACTIVE.
            tagged_unresolvable = [
                (wire, reason, "RELEASED") for wire, reason in unresolvable_r
            ] + [(wire, reason, "ACTIVE") for wire, reason in unresolvable_b]
            await _apply_wiring_pairs(
                str(reservation_id),
                release_by_switch,
                build_by_switch,
                tagged_unresolvable,
                ctx,
                get_db_session,
            )

        # L2 membership reconcile runs AFTER the L1 pass (Decision 4 ordering: L2 removes
        # land after L1 builds), always as a full reconcile against the intended wires.
        await _reconcile_l2_memberships(str(reservation_id), l2_intended_wires, ctx, get_db_session)

        # L3 adjacency reconcile runs AFTER the L2 pass (Decision 4 ordering: L3 deprovision
        # then provision, all after L2), always a full reconcile against the SAME intended
        # wires fetched once above (derive both layers from one fetch, never fetch twice).
        await _reconcile_l3_adjacency(str(reservation_id), l2_intended_wires, ctx, get_db_session)

    # Advance the monotonic marker: the version was processed (Decision 4/6), even if
    # the pass left FAILED rows for the Decision 6 retry channel.
    async with get_db_session() as db:
        await stamp_last_applied(db, reservation_id, fork_version)

    logger.info(
        "Applied wiring_changed version %s for reservation %s (%s)",
        fork_version,
        reservation_id,
        "full-reconcile" if full_reconcile else "carried-delta",
    )


async def _teardown_from_ledgers(
    reservation_id: str,
    ctx: "_FetchContext",
    get_db_session,
) -> None:
    """Release a reservation's applied wiring from the three ledgers (ADR 0009 phase 6).

    The terminal-transition teardown (cancelled/completed/failed). Instead of resolving
    teardown work from the device set (the legacy _resolve_l{1,2,3}_switch_operations
    resolvers, which keep serving provisioning until phase 7), release exactly what the
    ledgers record as applied. The reservation is ending, so every ACTIVE row is due for
    removal; the pass drives the SAME shared apply functions the wiring_changed reconcile
    and both retry channels use, so result gating, the issue #412 guard, FAILED
    intended-RELEASED rows on a driver failure (retryable through the phase-3
    direction-scoped channels, which discharges the issue #244 posture: a teardown driver
    failure is now visible and retryable instead of a silently stranded ACTIVE row),
    attempts accumulation, the L2 allocation lifecycle coupling, and the issue #20 pin
    semantics all come for free. `action_succeeded_for_reservation` is no longer consulted:
    an ACTIVE l1_connection_assignments row IS an applied pair (a connect only flips ACTIVE
    on a gated driver success), so reading the ACTIVE set gives the issue #244
    applied-state-only guarantee directly.

    Ordering mirrors ADR 0009 Decision 4 in release form: L1 disconnects, then L2
    membership removes plus allocation frees, then L3 pin removals. Empty ledgers no-op
    with no driver call. The wiring freeze is deliberately NOT consulted here: the freeze
    gates a LATE wiring_changed event and build-direction retries, never the terminal
    teardown pass itself. The caller sets frozen AFTER this pass, preserving the
    pre-phase-6 ordering (teardown, then dynamic teardown, then freeze).
    """
    from app.services.l1_assignment_service import active_assignments_for_reservation
    from app.services.l2_membership_service import active_memberships_for_reservation
    from app.services.route_service import (
        get_effective_pinned_routes,
        get_route_assignments,
    )

    res_str = str(reservation_id)

    # L1: disconnect every ACTIVE cross-connect pair, verbatim from the ledger. A driver
    # failure lands a FAILED intended-RELEASED row (release-side of _apply_one_port_action);
    # a moved-then-stranded pair is idempotently skipped by the pair_needs_release gate.
    async with get_db_session() as db:
        l1_rows = await active_assignments_for_reservation(db, res_str)
    release_by_switch: dict[str, list[tuple[str, str, str | None]]] = {}
    for row in l1_rows:
        phys = str(row.physical_connection_id) if row.physical_connection_id else None
        release_by_switch.setdefault(str(row.switch_device_id), []).append(
            (row.port_a, row.port_b, phys)
        )
    if release_by_switch:
        await _apply_wiring_pairs(res_str, release_by_switch, {}, [], ctx, get_db_session)

    # L2: remove every ACTIVE membership, then release each now-orphaned allocation. The
    # allocation coupling runs inside _apply_l2_memberships after the whole membership pass,
    # so a fabric whose last member left is freed (Decision 4).
    async with get_db_session() as db:
        l2_rows = await active_memberships_for_reservation(db, res_str)
    if l2_rows:
        remove_alloc_ids = {row.vlan_assignment_id for row in l2_rows}
        vlan_by_alloc = await _vlan_ids_for(remove_alloc_ids, get_db_session)
        removes = [
            {
                "switch_device_id": str(row.switch_device_id),
                "port": row.port,
                "vlan_assignment_id": row.vlan_assignment_id,
                "vlan_id": vlan_by_alloc.get(row.vlan_assignment_id, 0),
            }
            for row in l2_rows
        ]
        await _apply_l2_memberships(res_str, removes, [], ctx, get_db_session)

    # L3: remove every ACTIVE pin using the PINNED route set verbatim (issue #20:
    # get_effective_pinned_routes, never a re-derived config). The adjacency-aware nuance
    # (a switch still serving another intended edge keeps its routes) is moot at terminal
    # time: the whole reservation ends, so every pinned switch is due for removal.
    async with get_db_session() as db:
        l3_rows = await get_route_assignments(db, res_str)
    deprovisions: list[dict] = []
    for row in l3_rows:
        switch_id = str(row.device_id)
        async with get_db_session() as db:
            pinned = await get_effective_pinned_routes(db, res_str, switch_id)
        deprovisions.append(
            {"device_id": switch_id, "routes": pinned if pinned is not None else row.routes}
        )
    if deprovisions:
        await _apply_l3_adjacency(res_str, deprovisions, [], ctx, get_db_session)


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

    # Dynamic-resource create (ADR 0004, issue #32) is its own event, entirely
    # separate from physical L1/L2/L3 provisioning. Inert until phase 3 publishes
    # provision_requested.
    if event_type == "reservation.provision_requested":
        await _handle_provision_requested(event_data, get_db_session, dedupe_key)
        return

    # Connection-driven L1 reconcile (ADR 0007, issue #345 P3b phase 3). A fork-save's
    # released/built delta (or a sweeper heal with no delta) applied hop-by-hop,
    # ordered by fork_version. Separate from device-set-driven L1/L2/L3 provisioning.
    if event_type == WIRING_CHANGED_EVENT:
        await handle_wiring_changed(event_data, get_db_session, dedupe_key)
        return

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

    # Health-poll tier transitions (issue #24): the same lifecycle events that
    # drive provisioning move the reservation's devices between the in-use and
    # idle polling tiers. Best-effort by design: the tier is a cadence hint, so
    # a failure here must never NAK an otherwise-processable provisioning
    # message; the absolute-UPDATE transition is re-applied by the next
    # lifecycle event touching the device.
    try:
        await apply_reservation_event_tiers(get_db_session, event_type, event_data)
    except Exception:
        logger.warning(
            "health tier transition failed for reservation %s", reservation_id, exc_info=True
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
                # Dynamic instances whose materialized device was removed are
                # torn down here (issue #32); instances still in the reservation
                # are left running.
                await _execute_dynamic_teardown(
                    reservation_id,
                    user_id,
                    get_db_session,
                    client,
                    removed_device_ids=removed_ids,
                )
        elif event_type in DYNAMIC_TEARDOWN_EVENTS:
            # ADR 0009 phase 6: terminal teardown (cancelled/completed/failed) releases
            # from the three wiring ledgers, not the device set. The legacy device-set
            # resolvers still serve provisioning (reservation.created) until phase 7; only
            # teardown is rerouted here. Reading the ACTIVE ledger set gives the issue #244
            # applied-state-only guarantee directly (an ACTIVE row IS an applied op), so no
            # only_applied_pairs / failed_cleanup / action_succeeded_for_reservation flag is
            # needed: the failed and cancelled/completed paths are one and the same.
            await _teardown_from_ledgers(reservation_id, ctx, get_db_session)

            # Dynamic-instance teardown (issue #32) runs after physical teardown
            # on complete/cancel/fail, never on reservation.created.
            await _execute_dynamic_teardown(reservation_id, user_id, get_db_session, client)

            # Freeze the wiring state on terminal events (ADR 0007 Decision 7,
            # issue #345 P3b phase 1). Once a reservation ends, a later wiring_changed
            # event must be a no-op; the frozen flag is that guard. Set AFTER the teardown
            # pass (unchanged ordering): the ledger teardown above is deliberately NOT gated
            # by frozen (the freeze gates late wiring_changed events and build-direction
            # retries, never the terminal teardown itself). Best-effort: a failure here must
            # never NAK an otherwise-processed teardown.
            from app.services.l1_assignment_service import freeze_reservation_wiring

            try:
                async with get_db_session() as db:
                    await freeze_reservation_wiring(db, reservation_id)
            except Exception:
                logger.warning(
                    "failed to freeze wiring state for reservation %s",
                    reservation_id,
                    exc_info=True,
                )
        else:
            # reservation.created: device-set-driven L1/L2/L3 provisioning. This legacy
            # build path (the _resolve_l{1,2,3}_switch_operations resolvers) is unchanged;
            # ADR 0009 phase 7 retires it in favor of an activation-staged wiring_changed.
            device_ids = event_data.get("device_ids", [])
            await _execute_switch_operations(
                device_ids,
                action,
                reservation_id,
                user_id,
                get_db_session,
                dedupe_key,
                ctx,
                only_applied_pairs=False,
            )
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
                    failed_cleanup=False,
                )
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
        # Non-retryable (e.g. VLAN pool exhausted, or a dynamic recipe's missing
        # template/hypervisor/secret): the condition is unchanged between
        # attempts, so retrying only burns max_deliver and delays the DLQ. Route
        # to the DLQ on the FIRST delivery with a distinct phrase.
        num_delivered = getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
        logger.error(
            "Permanent error processing NATS message; routing to DLQ on first delivery (no retry)",
            extra={
                "action": "nats_dlq_permanent",
                "delivered": num_delivered,
                "event": event_data.get("event"),
            },
            exc_info=exc,
        )
        await _publish_to_dlq(js, msg.data)
        # For a dead-lettered provision_requested, tell reservations the
        # provisioning failed so it can transition to FAILED and publish
        # reservation.failed (whose teardown handler owns instance cleanup); we
        # do NOT tear down here. No-op for every other event.
        await _maybe_post_provision_failure(event_data, str(exc))
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
            # Same failure callback as the permanent branch: a provision_requested
            # that exhausted retries reports failure so reservations fails the
            # reservation and its teardown handler cleans up. No-op otherwise.
            await _maybe_post_provision_failure(event_data, str(exc))
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
                # Heartbeat every fetched message while the batch is processed
                # sequentially (issue #317): a slow provisioning handler must not
                # let JetStream redeliver a message still in flight here, whether
                # it is the one executing or one still queued behind it in this
                # batch. Settled messages drop out of in_flight so their heartbeat
                # stops. Driver calls run off-loop (_run_sandbox) so this task is
                # actually scheduled while provisioning runs.
                in_flight = list(msgs)
                heartbeat = asyncio.create_task(
                    _keep_messages_alive(in_flight, NATS_HEARTBEAT_SECONDS)
                )
                try:
                    for msg in msgs:
                        try:
                            await process_reservation_message(
                                msg, js, handle_reservation_event, _get_db_session
                            )
                        except Exception:
                            # process_reservation_message never raises on its own; this
                            # catches ack/nak/publish failures so the loop keeps draining.
                            logger.error("Unexpected error in NATS consumer loop", exc_info=True)
                        finally:
                            # Stop heartbeating this message once it is settled.
                            try:
                                in_flight.remove(msg)
                            except ValueError:
                                pass
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass

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
