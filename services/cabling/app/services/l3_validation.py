"""Layer 3 routing-intent validation pass (ADR 0014 phase 1, issue #34).

``validate_canvas_l3`` is one of the two composable passes the topology validator
(``topology_validation.py``) combines (R1 review fix on 2ade362c). S10 review fix
(round 2 on 2ade362c): it no longer parses the canvas itself; the caller parses
ONCE via ``l3_intent.walk_l3_nodes`` and hands both the well-formed candidates and
the malformed entries in, so a caller that also needs the merged intent (e.g.
``fork_save_service.gate_l3_intent``) never parses the same canvas twice. Reasons
and evaluation order are ADR 0014 Decision 5 plus the phase 1 amendment's ninth
reason, ``l3_malformed``, and the round-2 amendment's tenth, ``l3_duplicate_route``
(see ``InvalidRoute`` for the exact vocabulary).

``touched_devices`` is supplied by the caller (R2 review fix): both devices of
every hop in ``resolve_canvas_wiring(canvas).specs``, port constraints honored,
transit devices included. This module never computes it itself and never runs its
own BFS.

Device type and config content come from inventory over the internal token via
``herd_common.internal_client.call_service`` (R6 review fix), batched per
validation call (chunks of ``_INVENTORY_BATCH_CHUNK_SIZE`` deduped ids, mirroring
inventory's own ``DEVICE_BATCH_MAX_IDS`` cap, fetched CONCURRENTLY across chunks,
S10) and memoized per device id (``L3InventoryContext``), with the per-switch
config-version reads issued concurrently via ``asyncio.gather`` bounded by an
``asyncio.Semaphore(8)`` (S10) once the batch has narrowed the config fetch down to
confirmed Layer 3 switches. S13 review fix (round 2): every inventory call carries
a 4s timeout and the whole pass runs under a 12s ``asyncio.wait_for`` budget, so a
slow or hung inventory never leaves a validate or save call hanging indefinitely.
An inventory transport error, unexpected non-2xx status, or the 12s deadline all
raise ``L3ConfigUnavailable``, which the caller maps to a 503
``{"error": "l3_config_unavailable"}`` (ADR 0014 Decision 5); a topology with no
``data.l3`` anywhere never triggers any inventory call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException
from herd_common.internal_client import InternalTokenAuth, call_service

from app.config import settings
from app.schemas.topology import InvalidRoute
from app.services.l3_intent import L3NodeCandidate, L3NodeMalformed, RouteSpec

LAYER_3_SWITCH_CONNECTION_TYPE = "Layer 3 Switch"

# Mirrors inventory's DEVICE_BATCH_MAX_IDS (services/inventory/app/routers/devices.py):
# cabling cannot import that constant across the service boundary, so this is kept in
# sync by hand. Chunking here means an unusually large single validation call (many
# L3-carrying switches) still respects inventory's own request-size cap.
_INVENTORY_BATCH_CHUNK_SIZE = 500

# S13 review fix, round 2: per-call inventory timeout and the whole-pass deadline.
_INVENTORY_CALL_TIMEOUT_SECONDS = 4.0
_L3_PASS_DEADLINE_SECONDS = 12.0

# S10 review fix, round 2: bounds the config-version fetch fan-out so a validation
# call touching many L3-carrying switches at once cannot open unbounded concurrent
# connections to inventory.
_CONFIG_FETCH_CONCURRENCY = 8


class L3ConfigUnavailable(Exception):
    """Inventory could not be asked, answered with an unexpected status, or the
    whole L3 pass exceeded its deadline (S13).

    Raised by ``L3InventoryContext`` on a transport failure, timeout, or a non-2xx
    response other than the 404 "no config version" means, and by
    ``validate_canvas_l3`` itself when the pass-wide deadline trips; the caller
    catches this and fails closed with 503 ``{"error": "l3_config_unavailable"}``.
    """


@dataclass
class L3InventoryContext:
    """Batches and memoizes the inventory reads one validation call needs.

    ``device_types`` is fetched once, in chunked (and, S10, concurrently issued)
    batch calls, for every device id the caller passes to ``load_device_types``.
    ``configs`` and ``config_version_ids`` memoize per device id together, fetched
    concurrently (bounded by ``_CONFIG_FETCH_CONCURRENCY``) in ``load_configs``; a
    device fetched twice within one validation call only ever costs one HTTP round
    trip.
    """

    _device_types: dict[uuid.UUID, str | None] = field(default_factory=dict)
    _configs: dict[uuid.UUID, dict | None] = field(default_factory=dict)
    # S6 review fix, round 2: the config VERSION id each device's config dict came
    # from, so the save path can stamp it onto ForkL3Route.validated_config_version_id
    # for the routes it actually judged against that version.
    _config_version_ids: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)

    async def load_device_types(self, device_ids: list[uuid.UUID]) -> None:
        """Batch-fetch connection_type for every id not already known.

        A no-op (no HTTP call) when every id is already memoized or the list is
        empty, so a validation call with no L3-carrying switch makes no inventory
        call at all. Chunks (when the deduped id count exceeds inventory's cap)
        are fetched CONCURRENTLY (S10), not one after another.
        """
        missing = list(dict.fromkeys(d for d in device_ids if d not in self._device_types))
        if not missing:
            return
        chunks = [
            missing[start : start + _INVENTORY_BATCH_CHUNK_SIZE]
            for start in range(0, len(missing), _INVENTORY_BATCH_CHUNK_SIZE)
        ]
        results = await asyncio.gather(*(self._fetch_device_type_chunk(c) for c in chunks))
        found: set[uuid.UUID] = set()
        for chunk_found in results:
            found.update(chunk_found)
        # A requested id inventory did not return (deleted between canvas save and
        # validate) memoizes as None, the same "not a router" outcome an explicit
        # non-Layer-3-Switch type produces, rather than re-fetching it forever.
        for device_id in missing:
            if device_id not in found:
                self._device_types[device_id] = None

    async def _fetch_device_type_chunk(self, chunk: list[uuid.UUID]) -> set[uuid.UUID]:
        try:
            resp = await call_service(
                settings.inventory_service_url,
                "POST",
                "/internal/devices/batch",
                json_body={"device_ids": [str(d) for d in chunk]},
                timeout=_INVENTORY_CALL_TIMEOUT_SECONDS,
                auth=InternalTokenAuth(token=settings.internal_api_token),
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            raise L3ConfigUnavailable(f"inventory device batch fetch failed: {exc}") from exc
        if resp.status_code >= 400:
            raise L3ConfigUnavailable(f"inventory device batch fetch returned {resp.status_code}")
        found: set[uuid.UUID] = set()
        for item in resp.json():
            device_id = uuid.UUID(str(item["id"]))
            self._device_types[device_id] = item.get("connection_type")
            found.add(device_id)
        return found

    def connection_type(self, device_id: uuid.UUID) -> str | None:
        return self._device_types.get(device_id)

    async def load_configs(self, device_ids: list[uuid.UUID]) -> None:
        """Concurrently fetch and memoize the latest config (and its version id,
        S6) for every id not already known, bounded by
        ``asyncio.Semaphore(_CONFIG_FETCH_CONCURRENCY)`` (S10). The first failure
        (transport, non-2xx other than 404, or timeout) propagates as
        ``L3ConfigUnavailable``, aborting whatever sibling fetches are still in
        flight, since a whole validation call fails closed together.
        """
        missing = list(dict.fromkeys(d for d in device_ids if d not in self._configs))
        if not missing:
            return

        semaphore = asyncio.Semaphore(_CONFIG_FETCH_CONCURRENCY)

        async def _fetch_and_store(device_id: uuid.UUID) -> None:
            async with semaphore:
                config, version_id = await self._fetch_one_config(device_id)
            self._configs[device_id] = config
            self._config_version_ids[device_id] = version_id

        await asyncio.gather(*(_fetch_and_store(d) for d in missing))

    async def _fetch_one_config(self, device_id: uuid.UUID) -> tuple[dict | None, uuid.UUID | None]:
        try:
            resp = await call_service(
                settings.inventory_service_url,
                "GET",
                f"/devices/{device_id}/config-versions/latest/internal",
                timeout=_INVENTORY_CALL_TIMEOUT_SECONDS,
                auth=InternalTokenAuth(token=settings.internal_api_token),
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            raise L3ConfigUnavailable(f"inventory config fetch failed: {exc}") from exc
        if resp.status_code == 404:
            return None, None
        if resp.status_code >= 400:
            raise L3ConfigUnavailable(f"inventory config fetch returned {resp.status_code}")
        body = resp.json() or {}
        version_id = uuid.UUID(str(body["id"])) if body.get("id") else None
        return body.get("config"), version_id

    def config(self, device_id: uuid.UUID) -> dict | None:
        return self._configs.get(device_id)

    def config_version_id(self, device_id: uuid.UUID) -> uuid.UUID | None:
        return self._config_version_ids.get(device_id)


def _usable_interfaces(raw_interfaces: object) -> dict[str, str | None]:
    """Extract a name-to-ip map from a config's ``interfaces`` list, tolerating a
    driver-published schema that stores any shape there (R6): a non-list, or a
    non-dict/no-name entry, is skipped rather than raising, so a garbled config
    reports ``l3_switch_unconfigured`` (no usable interface names) instead of a
    500.
    """
    if not isinstance(raw_interfaces, list):
        return {}
    result: dict[str, str | None] = {}
    for entry in raw_interfaces:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name:
            result[name] = entry.get("ip")
    return result


def _validate_one_route(route: RouteSpec, interfaces: dict[str, str | None]) -> str | None:
    """Evaluate one route's per-route reasons in order; return the first that
    applies, or None when the route is clean. Interface routes (no next_hop) skip
    every next-hop check. S10: ``next_hop`` is parsed to an ``ip_address`` once
    and reused for both the shape check and the interface-membership check.
    """
    try:
        ipaddress.ip_network(route.destination, strict=False)
    except ValueError:
        return "l3_bad_destination"

    next_hop_addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if route.next_hop is not None:
        try:
            next_hop_addr = ipaddress.ip_address(route.next_hop)
        except ValueError:
            return "l3_bad_next_hop"

    if route.interface not in interfaces:
        return "l3_unknown_interface"

    if next_hop_addr is None:
        return None

    ip_value = interfaces.get(route.interface)
    iface = None
    if ip_value:
        try:
            iface = ipaddress.ip_interface(ip_value)
        except ValueError:
            iface = None
    if iface is None or iface.network.prefixlen == iface.network.max_prefixlen:
        return "l3_next_hop_unverifiable"
    if next_hop_addr not in iface.network:
        return "l3_next_hop_outside_interface"
    return None


async def validate_switch_l3(
    node_id: str,
    device_id: uuid.UUID,
    routes: list[RouteSpec],
    *,
    ctx: L3InventoryContext,
    touched_devices: set[uuid.UUID],
) -> tuple[list[InvalidRoute], uuid.UUID | None]:
    """Evaluate one already-parsed, non-empty route list; returns its InvalidRoute
    entries plus the config version id the routes were judged against (S6),
    ``None`` when no per-route judgement actually ran.

    Switch-level checks (not a router, unconfigured, unattached) stop at the first
    one that applies and suppress every per-route check for this switch (ADR 0014
    Decision 5); once past them every route is evaluated independently and each
    route with a problem contributes one entry. The shape check (``l3_malformed``)
    and the duplicate-collapse check (``l3_duplicate_route``, S12) already ran in
    the caller (``validate_canvas_l3``) before this is ever called.
    """
    if ctx.connection_type(device_id) != LAYER_3_SWITCH_CONNECTION_TYPE:
        return (
            [
                InvalidRoute(
                    node_id=node_id, device_id=device_id, index=None, reason="l3_not_a_router"
                )
            ],
            None,
        )

    config = ctx.config(device_id)
    interfaces = _usable_interfaces((config or {}).get("interfaces"))
    if config is None or not interfaces:
        return (
            [
                InvalidRoute(
                    node_id=node_id,
                    device_id=device_id,
                    index=None,
                    reason="l3_switch_unconfigured",
                )
            ],
            None,
        )

    if device_id not in touched_devices:
        return (
            [
                InvalidRoute(
                    node_id=node_id, device_id=device_id, index=None, reason="l3_switch_unattached"
                )
            ],
            None,
        )

    results: list[InvalidRoute] = []
    for route in routes:
        reason = _validate_one_route(route, interfaces)
        if reason is not None:
            results.append(
                InvalidRoute(
                    node_id=node_id, device_id=device_id, index=route.source_index, reason=reason
                )
            )
    return results, ctx.config_version_id(device_id)


@dataclass(frozen=True)
class L3ValidationResult:
    """``validate_canvas_l3``'s outcome: every InvalidRoute entry (switch-level,
    per-route, ``l3_malformed``, and the informational ``l3_duplicate_route``,
    S12), plus the per-device inventory config version id each judged switch's
    routes were actually checked against (S6; absent for a switch that was never
    reached far enough to be judged, e.g. ``l3_not_a_router``).
    """

    invalid_routes: list[InvalidRoute]
    validated_config_version_ids: dict[uuid.UUID, uuid.UUID | None]


def route_causes_invalid(entry: InvalidRoute) -> bool:
    """Whether one InvalidRoute entry should make ``valid`` false / a save gate
    refuse (S12): ``l3_duplicate_route`` is informational only, so it is
    excluded here; every other reason counts.
    """
    return entry.reason != "l3_duplicate_route"


async def validate_canvas_l3(
    candidates: list[L3NodeCandidate],
    malformed: list[L3NodeMalformed],
    touched_devices: set[uuid.UUID],
) -> L3ValidationResult:
    """Run the L3 validation pass (ADR 0014 Decision 5) over already-parsed
    candidates and malformed entries (S10 review fix, round 2: the caller parses
    the canvas exactly once via ``l3_intent.walk_l3_nodes`` and passes both
    results in here; this function never parses ``data.l3`` itself).

    ``l3_malformed`` entries (one per ``malformed`` item) and ``l3_duplicate_route``
    entries (S12, one per collapsed duplicate in ``candidates``) are always
    included, regardless of switch-level outcome, so nothing vanishes silently;
    neither counts toward ``valid``/gate refusal (see ``route_causes_invalid``).

    Returns ``invalid_routes=[]`` and no inventory call at all when both
    ``candidates`` and ``malformed`` are empty. Raises HTTPException 503
    ``{"error": "l3_config_unavailable"}`` on an inventory transport failure,
    unexpected non-2xx status, or the whole-pass deadline (S13,
    ``_L3_PASS_DEADLINE_SECONDS``).
    """
    invalid_routes: list[InvalidRoute] = [
        InvalidRoute(
            node_id=node_id,
            device_id=device_id,
            index=None,
            reason="l3_malformed",
            detail=exc.message,
        )
        for node_id, device_id, exc in malformed
    ]
    for node_id, device_id, _routes, duplicate_indices in candidates:
        for dup_index in duplicate_indices:
            invalid_routes.append(
                InvalidRoute(
                    node_id=node_id,
                    device_id=device_id,
                    index=dup_index,
                    reason="l3_duplicate_route",
                )
            )

    if not candidates:
        return L3ValidationResult(invalid_routes=invalid_routes, validated_config_version_ids={})

    async def _run() -> L3ValidationResult:
        ctx = L3InventoryContext()
        await ctx.load_device_types([device_id for _, device_id, _, _ in candidates])
        routers = [
            (node_id, device_id, routes)
            for node_id, device_id, routes, _dup in candidates
            if ctx.connection_type(device_id) == LAYER_3_SWITCH_CONNECTION_TYPE
        ]
        await ctx.load_configs(list(dict.fromkeys(device_id for _, device_id, _ in routers)))

        validated_config_version_ids: dict[uuid.UUID, uuid.UUID | None] = {}
        for node_id, device_id, routes, _dup in candidates:
            entries, config_version_id = await validate_switch_l3(
                node_id, device_id, routes, ctx=ctx, touched_devices=touched_devices
            )
            invalid_routes.extend(entries)
            if config_version_id is not None:
                validated_config_version_ids[device_id] = config_version_id
        return L3ValidationResult(invalid_routes, validated_config_version_ids)

    try:
        return await asyncio.wait_for(_run(), timeout=_L3_PASS_DEADLINE_SECONDS)
    except L3ConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error": "l3_config_unavailable"}) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail={"error": "l3_config_unavailable"}) from exc


__all__ = [
    "L3ConfigUnavailable",
    "L3InventoryContext",
    "L3ValidationResult",
    "LAYER_3_SWITCH_CONNECTION_TYPE",
    "route_causes_invalid",
    "validate_canvas_l3",
    "validate_switch_l3",
]
