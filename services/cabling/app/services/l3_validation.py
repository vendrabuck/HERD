"""Layer 3 routing-intent validation pass (ADR 0014 phase 1, issue #34).

``validate_canvas_l3`` is one of the two composable passes the topology validator
(``topology_validation.py``) combines (R1 review fix on 2ade362c); it walks every
device node carrying ``data.l3``, reusing the canvas parser in ``l3_intent.py`` per
node so a malformed switch reports and does not abort validation of the rest of the
topology. Reasons and evaluation order are ADR 0014 Decision 5 plus the phase 1
amendment's ninth reason, ``l3_malformed`` (see ``InvalidRoute`` for the exact
vocabulary).

``touched_devices`` is supplied by the caller (R2 review fix): both devices of
every hop in ``resolve_canvas_wiring(canvas).specs``, port constraints honored,
transit devices included. This module never computes it itself and never runs its
own BFS.

Device type and config content come from inventory over the internal token via
``herd_common.internal_client.call_service`` (R6 review fix), batched per
validation call (one ``POST /internal/devices/batch`` per chunk of
``_INVENTORY_BATCH_CHUNK_SIZE`` deduped ids, mirroring inventory's own
``DEVICE_BATCH_MAX_IDS`` cap) and memoized per device id (``L3InventoryContext``),
with the per-switch config-version reads issued concurrently via ``asyncio.gather``
once the batch has narrowed the config fetch down to confirmed Layer 3 switches. An
inventory transport error or non-2xx status other than the 404 that means "no
config version yet" (``l3_switch_unconfigured``) raises ``L3ConfigUnavailable``,
which the caller maps to a 503 ``{"error": "l3_config_unavailable"}`` (ADR 0014
Decision 5); a topology with no ``data.l3`` anywhere never triggers any inventory
call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException
from herd_common.internal_client import InternalTokenAuth, call_service
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.topology import InvalidRoute
from app.services.canvas_nodes import node_to_device_map
from app.services.l3_intent import L3IntentMalformed, RouteSpec, parse_node_l3

LAYER_3_SWITCH_CONNECTION_TYPE = "Layer 3 Switch"

# Mirrors inventory's DEVICE_BATCH_MAX_IDS (services/inventory/app/routers/devices.py):
# cabling cannot import that constant across the service boundary, so this is kept in
# sync by hand. Chunking here means an unusually large single validation call (many
# L3-carrying switches) still respects inventory's own request-size cap.
_INVENTORY_BATCH_CHUNK_SIZE = 500


class L3ConfigUnavailable(Exception):
    """Inventory could not be asked, or answered with an unexpected status.

    Raised by ``L3InventoryContext`` on a transport failure or a non-2xx response
    other than the 404 "no config version" means; the caller catches this and fails
    closed with 503 ``{"error": "l3_config_unavailable"}``.
    """


@dataclass
class L3InventoryContext:
    """Batches and memoizes the inventory reads one validation call needs.

    ``device_types`` is fetched once, in chunked batch calls, for every device id
    the caller passes to ``load_device_types``. ``configs`` memoizes per device id,
    fetched concurrently via ``asyncio.gather`` in ``load_configs``; a device
    fetched twice within one validation call only ever costs one HTTP round trip.
    """

    _device_types: dict[uuid.UUID, str | None] = field(default_factory=dict)
    _configs: dict[uuid.UUID, dict | None] = field(default_factory=dict)

    async def load_device_types(self, device_ids: list[uuid.UUID]) -> None:
        """Batch-fetch connection_type for every id not already known.

        A no-op (no HTTP call) when every id is already memoized or the list is
        empty, so a validation call with no L3-carrying switch makes no inventory
        call at all.
        """
        missing = list(dict.fromkeys(d for d in device_ids if d not in self._device_types))
        if not missing:
            return
        found: set[uuid.UUID] = set()
        for start in range(0, len(missing), _INVENTORY_BATCH_CHUNK_SIZE):
            chunk = missing[start : start + _INVENTORY_BATCH_CHUNK_SIZE]
            try:
                resp = await call_service(
                    settings.inventory_service_url,
                    "POST",
                    "/internal/devices/batch",
                    json_body={"device_ids": [str(d) for d in chunk]},
                    timeout=10.0,
                    auth=InternalTokenAuth(token=settings.internal_api_token),
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                raise L3ConfigUnavailable(f"inventory device batch fetch failed: {exc}") from exc
            if resp.status_code >= 400:
                raise L3ConfigUnavailable(
                    f"inventory device batch fetch returned {resp.status_code}"
                )
            for item in resp.json():
                device_id = uuid.UUID(str(item["id"]))
                self._device_types[device_id] = item.get("connection_type")
                found.add(device_id)
        # A requested id inventory did not return (deleted between canvas save and
        # validate) memoizes as None, the same "not a router" outcome an explicit
        # non-Layer-3-Switch type produces, rather than re-fetching it forever.
        for device_id in missing:
            if device_id not in found:
                self._device_types[device_id] = None

    def connection_type(self, device_id: uuid.UUID) -> str | None:
        return self._device_types.get(device_id)

    async def load_configs(self, device_ids: list[uuid.UUID]) -> None:
        """Concurrently fetch and memoize the latest config for every id not
        already known (R6: ``asyncio.gather`` over the deduped set). The first
        failure (transport or non-2xx other than 404) propagates as
        ``L3ConfigUnavailable``, aborting whatever sibling fetches are still
        in flight, since a whole validation call fails closed together.
        """
        missing = list(dict.fromkeys(d for d in device_ids if d not in self._configs))
        if not missing:
            return

        async def _fetch_and_store(device_id: uuid.UUID) -> None:
            self._configs[device_id] = await self._fetch_one_config(device_id)

        await asyncio.gather(*(_fetch_and_store(d) for d in missing))

    async def _fetch_one_config(self, device_id: uuid.UUID) -> dict | None:
        try:
            resp = await call_service(
                settings.inventory_service_url,
                "GET",
                f"/devices/{device_id}/config-versions/latest/internal",
                timeout=10.0,
                auth=InternalTokenAuth(token=settings.internal_api_token),
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            raise L3ConfigUnavailable(f"inventory config fetch failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise L3ConfigUnavailable(f"inventory config fetch returned {resp.status_code}")
        return (resp.json() or {}).get("config")

    def config(self, device_id: uuid.UUID) -> dict | None:
        return self._configs.get(device_id)


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
    every next-hop check."""
    try:
        ipaddress.ip_network(route.destination, strict=False)
    except ValueError:
        return "l3_bad_destination"

    if route.next_hop is not None:
        try:
            ipaddress.ip_address(route.next_hop)
        except ValueError:
            return "l3_bad_next_hop"

    if route.interface not in interfaces:
        return "l3_unknown_interface"

    if route.next_hop is None:
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
    if ipaddress.ip_address(route.next_hop) not in iface.network:
        return "l3_next_hop_outside_interface"
    return None


def _split_l3_nodes(
    canvas: dict,
) -> tuple[list[tuple[str, uuid.UUID, list[RouteSpec]]], list[InvalidRoute]]:
    """Walk the canvas once, separating well-formed L3-carrying nodes (with their
    already-parsed, non-empty route list) from malformed ones. R10: a node whose
    ``data.l3`` parses to an empty route list is skipped entirely, exactly like
    ``parse_l3_intent`` treats it as no intent (no switch-level checks, no
    inventory involvement).
    """
    node_to_device = node_to_device_map(canvas)
    nodes = canvas.get("nodes") or []
    candidates: list[tuple[str, uuid.UUID, list[RouteSpec]]] = []
    malformed: list[InvalidRoute] = []
    for node in nodes:
        node_id = node.get("id")
        if node_id is None:
            continue
        device_id = node_to_device.get(node_id)
        if device_id is None:
            continue
        l3_data = (node.get("data") or {}).get("l3")
        if l3_data is None:
            continue
        try:
            routes = parse_node_l3(node_id, l3_data)
        except L3IntentMalformed as exc:
            malformed.append(
                InvalidRoute(
                    node_id=node_id,
                    device_id=device_id,
                    index=None,
                    reason="l3_malformed",
                    detail=exc.message,
                )
            )
            continue
        if not routes:
            continue
        candidates.append((node_id, device_id, routes))
    return candidates, malformed


async def validate_switch_l3(
    node_id: str,
    device_id: uuid.UUID,
    routes: list[RouteSpec],
    *,
    ctx: L3InventoryContext,
    touched_devices: set[uuid.UUID],
) -> list[InvalidRoute]:
    """Evaluate one already-parsed, non-empty route list; returns its InvalidRoute
    entries.

    Switch-level checks (not a router, unconfigured, unattached) stop at the first
    one that applies and suppress every per-route check for this switch (ADR 0014
    Decision 5); once past them every route is evaluated independently and each
    route with a problem contributes one entry. The shape check (``l3_malformed``)
    already ran in ``_split_l3_nodes`` before this is ever called.
    """
    if ctx.connection_type(device_id) != LAYER_3_SWITCH_CONNECTION_TYPE:
        return [
            InvalidRoute(node_id=node_id, device_id=device_id, index=None, reason="l3_not_a_router")
        ]

    config = ctx.config(device_id)
    interfaces = _usable_interfaces((config or {}).get("interfaces"))
    if config is None or not interfaces:
        return [
            InvalidRoute(
                node_id=node_id,
                device_id=device_id,
                index=None,
                reason="l3_switch_unconfigured",
            )
        ]

    if device_id not in touched_devices:
        return [
            InvalidRoute(
                node_id=node_id, device_id=device_id, index=None, reason="l3_switch_unattached"
            )
        ]

    results: list[InvalidRoute] = []
    for index, route in enumerate(routes):
        reason = _validate_one_route(route, interfaces)
        if reason is not None:
            results.append(
                InvalidRoute(node_id=node_id, device_id=device_id, index=index, reason=reason)
            )
    return results


async def validate_canvas_l3(
    canvas: dict,
    db: AsyncSession,
    touched_devices: set[uuid.UUID],
) -> list[InvalidRoute]:
    """Run the L3 validation pass (ADR 0014 Decision 5) over one canvas.

    ``db`` is accepted for signature symmetry with ``validate_canvas_edges`` (the
    topology validator's other composable pass); this pass never touches the local
    database, only inventory over HTTP. Returns ``[]`` (no inventory call at all)
    when the canvas carries no well-formed, non-empty L3 intent anywhere. Raises
    HTTPException 503 ``{"error": "l3_config_unavailable"}`` on an inventory
    transport failure or unexpected non-2xx status (``L3ConfigUnavailable``).
    """
    candidates, malformed = _split_l3_nodes(canvas)
    if not candidates:
        return malformed

    ctx = L3InventoryContext()
    try:
        await ctx.load_device_types([device_id for _, device_id, _ in candidates])
        routers = [
            (node_id, device_id, routes)
            for node_id, device_id, routes in candidates
            if ctx.connection_type(device_id) == LAYER_3_SWITCH_CONNECTION_TYPE
        ]
        await ctx.load_configs(list(dict.fromkeys(device_id for _, device_id, _ in routers)))

        invalid_routes: list[InvalidRoute] = list(malformed)
        for node_id, device_id, routes in candidates:
            invalid_routes.extend(
                await validate_switch_l3(
                    node_id, device_id, routes, ctx=ctx, touched_devices=touched_devices
                )
            )
        return invalid_routes
    except L3ConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error": "l3_config_unavailable"}) from exc


__all__ = [
    "L3ConfigUnavailable",
    "L3InventoryContext",
    "LAYER_3_SWITCH_CONNECTION_TYPE",
    "validate_canvas_l3",
    "validate_switch_l3",
]
