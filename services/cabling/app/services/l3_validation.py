"""Layer 3 routing-intent validation pass (ADR 0014 phase 1, issue #34).

Extends ``_run_topology_validation`` (``routes/topologies.py``) with a pass over
every device node carrying ``data.l3``, reusing the canvas parser in
``l3_intent.py`` per node so a malformed switch reports and does not abort
validation of the rest of the topology. Reasons and evaluation order are ADR 0014
Decision 5 (see ``InvalidRoute`` for the exact vocabulary).

Device type and config content come from inventory over the internal token,
batched per validation call (one ``POST /internal/devices/batch`` for every
L3-carrying switch's connection_type) and memoized per device id
(``L3InventoryContext``), since inventory's existing config-version-latest route has
no batch form. An inventory transport error or non-2xx status other than the 404
that means "no config version yet" (``l3_switch_unconfigured``) raises
``L3ConfigUnavailable``, which both validate routes map to a 503
``{"error": "l3_config_unavailable"}`` (ADR 0014 Decision 5); a topology with no
``data.l3`` anywhere never triggers any inventory call.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.services.l3_intent import L3IntentMalformed, RouteSpec, parse_node_l3

LAYER_3_SWITCH_CONNECTION_TYPE = "Layer 3 Switch"


class L3ConfigUnavailable(Exception):
    """Inventory could not be asked, or answered with an unexpected status.

    Raised by ``L3InventoryContext`` on a transport failure or a non-2xx response
    other than the 404 "no config version" means; both validate routes catch this
    and fail closed with 503 ``{"error": "l3_config_unavailable"}``.
    """


@dataclass
class L3InventoryContext:
    """Batches and memoizes the inventory reads one validation call needs.

    ``device_types`` is fetched once, in one batch call, for every device id the
    caller passes to ``load_device_types``. ``latest_config`` memoizes per device
    id on first fetch (there is no batch form of that route); a device fetched
    twice within one validation call only ever costs one HTTP round trip.
    """

    _device_types: dict[uuid.UUID, str | None] = field(default_factory=dict)
    _configs: dict[uuid.UUID, dict | None] = field(default_factory=dict)
    _types_loaded: bool = False

    async def load_device_types(self, device_ids: list[uuid.UUID]) -> None:
        """Batch-fetch connection_type for every id not already known.

        A no-op (no HTTP call) when every id is already memoized or the list is
        empty, so a validation call with no L3-carrying switch makes no inventory
        call at all.
        """
        missing = [d for d in device_ids if d not in self._device_types]
        if not missing:
            return
        url = f"{settings.inventory_service_url.rstrip('/')}/internal/devices/batch"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={"X-Internal-Token": settings.internal_api_token},
                    json={"device_ids": [str(d) for d in missing]},
                )
        except httpx.HTTPError as exc:
            raise L3ConfigUnavailable(f"inventory device batch fetch failed: {exc}") from exc
        if resp.status_code >= 400:
            raise L3ConfigUnavailable(f"inventory device batch fetch returned {resp.status_code}")
        found: set[uuid.UUID] = set()
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

    async def latest_config(self, device_id: uuid.UUID) -> dict | None:
        """Return the device's latest config version's `config` dict, or None.

        None means "no config version yet" (inventory 404), which the caller maps
        to ``l3_switch_unconfigured``. Any other non-2xx or transport failure
        raises ``L3ConfigUnavailable``.
        """
        if device_id in self._configs:
            return self._configs[device_id]
        url = (
            f"{settings.inventory_service_url.rstrip('/')}"
            f"/devices/{device_id}/config-versions/latest/internal"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url, headers={"X-Internal-Token": settings.internal_api_token}
                )
        except httpx.HTTPError as exc:
            raise L3ConfigUnavailable(f"inventory config fetch failed: {exc}") from exc
        if resp.status_code == 404:
            self._configs[device_id] = None
            return None
        if resp.status_code >= 400:
            raise L3ConfigUnavailable(f"inventory config fetch returned {resp.status_code}")
        config = (resp.json() or {}).get("config")
        self._configs[device_id] = config
        return config


def _validate_one_route(
    index: int, route: RouteSpec, interfaces: dict[str, str | None]
) -> str | None:
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


async def validate_switch_l3(
    node_id: str,
    device_id: uuid.UUID,
    l3_data: object,
    *,
    ctx: L3InventoryContext,
    touched_devices: set[uuid.UUID],
) -> list["InvalidRoute"]:  # noqa: F821 (imported lazily below to avoid a cycle)
    """Evaluate one device node's L3 intent; returns its InvalidRoute entries.

    Switch-level checks (malformed shape, not a router, unconfigured, unattached)
    stop at the first one that applies and suppress every per-route check for this
    switch (ADR 0014 Decision 5); once past them every route in the list is
    evaluated independently and each route with a problem contributes one entry.
    """
    from app.schemas.topology import InvalidRoute

    try:
        routes = parse_node_l3(node_id, l3_data)
    except L3IntentMalformed as exc:
        return [
            InvalidRoute(
                node_id=node_id,
                device_id=device_id,
                index=None,
                reason="l3_malformed",
                detail=exc.message,
            )
        ]

    if ctx.connection_type(device_id) != LAYER_3_SWITCH_CONNECTION_TYPE:
        return [
            InvalidRoute(node_id=node_id, device_id=device_id, index=None, reason="l3_not_a_router")
        ]

    config = await ctx.latest_config(device_id)
    raw_interfaces = (config or {}).get("interfaces") or []
    if config is None or not raw_interfaces:
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

    interfaces = {
        entry.get("name"): entry.get("ip") for entry in raw_interfaces if entry.get("name")
    }

    results: list[InvalidRoute] = []
    for index, route in enumerate(routes):
        reason = _validate_one_route(index, route, interfaces)
        if reason is not None:
            results.append(
                InvalidRoute(node_id=node_id, device_id=device_id, index=index, reason=reason)
            )
    return results


__all__ = [
    "L3ConfigUnavailable",
    "L3InventoryContext",
    "LAYER_3_SWITCH_CONNECTION_TYPE",
    "validate_switch_l3",
]
