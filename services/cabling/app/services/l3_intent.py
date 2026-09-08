"""Canvas parser for Layer 3 routing intent (ADR 0014 phase 1, issue #34).

A device node whose device is a `Layer 3 Switch` may carry `data.l3 = {"routes":
[...]}` in canvas_data (ADR 0014 Decision 1). This module is the single place that
reads that shape off a canvas and turns it into ``RouteSpec`` objects; nothing else
in cabling parses ``data.l3`` directly.

``route_key`` is the route identity used both by the fork-save set reconcile
(``fork_save_service.save_fork``, ``fork_service.create_fork``) and by the
``fork_l3_routes`` unique constraint: it packs the same three fields execution's
``_route_run_identity`` (``services/execution/app/services/nats_consumer.py:413-424``)
already treats as a route's identity (destination, interface, next_hop), just as one
string instead of a tuple.

Element and dynamic-placeholder nodes never carry ``l3``: this module only ever
looks at ``data.l3`` on nodes that ``node_to_device_map`` resolves to a real device,
so a stray ``l3`` key on a non-device node is silently never read (the same posture
``node_to_device_map`` already takes toward every other device-only field).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.services.fork_save_service import node_to_device_map

_ALLOWED_ROUTE_KEYS = frozenset({"destination", "next_hop", "interface", "virtual_router"})
_MAX_FIELD_LENGTH = 64


class L3IntentMalformed(Exception):
    """Raised when one device node's ``data.l3`` does not match the ADR 0014 shape.

    ``node_id`` is the React Flow node id carrying the bad shape; ``message`` is a
    short, human-readable reason. The write paths (fork save, fork create) map this
    to a 422 ``{"error": "l3_intent_malformed", "node_id", "message"}``; the
    validation pass (``l3_validation.py``) instead catches it per node and reports it
    as an ``l3_malformed`` ``InvalidRoute`` entry so one malformed switch does not
    stop validation of the rest of the topology.
    """

    def __init__(self, node_id: str, message: str):
        self.node_id = node_id
        self.message = message
        super().__init__(f"{node_id}: {message}")


@dataclass(frozen=True)
class RouteSpec:
    """One parsed route from a device node's ``data.l3.routes`` entry.

    Mirrors the ``Layer 3 Switch`` config schema's ``routes`` item shape
    (``services/common/herd_common/device_config.py``) plus the optional
    ``virtual_router`` grouping ADR 0014 adds. ``virtual_router`` is not validated
    against the device's own config in phase 1 (ADR 0014 Decision 5, a named
    follow-up).
    """

    destination: str
    next_hop: str | None
    interface: str
    virtual_router: str | None

    @property
    def route_key(self) -> str:
        """The reconcile/uniqueness identity: destination, interface, next_hop."""
        return f"{self.destination}|{self.interface}|{self.next_hop or ''}"


def _require_string(node_id: str, value: Any, field_name: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise L3IntentMalformed(node_id, f"'{field_name}' is required")
        return None
    if not isinstance(value, str):
        raise L3IntentMalformed(node_id, f"'{field_name}' must be a string")
    if required and not value:
        raise L3IntentMalformed(node_id, f"'{field_name}' must be a non-empty string")
    if len(value) > _MAX_FIELD_LENGTH:
        raise L3IntentMalformed(
            node_id, f"'{field_name}' must be at most {_MAX_FIELD_LENGTH} characters"
        )
    return value


def parse_node_l3(node_id: str, l3_data: Any) -> list[RouteSpec]:
    """Parse one node's ``data.l3`` value into deduplicated ``RouteSpec`` rows.

    Raises ``L3IntentMalformed`` for any shape violation (ADR 0014 Decision 1):
    ``l3`` must be an object whose only key is ``routes``; ``routes`` must be a
    list; each item must be an object with exactly the allowed keys, ``destination``
    and ``interface`` required non-empty strings (max 64 chars), ``next_hop`` and
    ``virtual_router`` optional strings (max 64 chars) or null.

    Duplicate ``route_key`` values within the list collapse to the first
    occurrence rather than raising, matching the resolver's own set semantics.
    """
    if not isinstance(l3_data, dict) or set(l3_data.keys()) != {"routes"}:
        raise L3IntentMalformed(node_id, "'l3' must be an object with exactly one key: 'routes'")

    routes_raw = l3_data["routes"]
    if not isinstance(routes_raw, list):
        raise L3IntentMalformed(node_id, "'l3.routes' must be a list")

    parsed: list[RouteSpec] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(routes_raw):
        if not isinstance(item, dict):
            raise L3IntentMalformed(node_id, f"route at index {index} must be an object")
        extra_keys = set(item.keys()) - _ALLOWED_ROUTE_KEYS
        if extra_keys:
            raise L3IntentMalformed(
                node_id,
                f"route at index {index} has unexpected key(s): {sorted(extra_keys)}",
            )
        destination = _require_string(
            node_id, item.get("destination"), "destination", required=True
        )
        interface = _require_string(node_id, item.get("interface"), "interface", required=True)
        next_hop = _require_string(node_id, item.get("next_hop"), "next_hop", required=False)
        virtual_router = _require_string(
            node_id, item.get("virtual_router"), "virtual_router", required=False
        )
        spec = RouteSpec(
            destination=destination,
            next_hop=next_hop,
            interface=interface,
            virtual_router=virtual_router,
        )
        if spec.route_key in seen_keys:
            continue
        seen_keys.add(spec.route_key)
        parsed.append(spec)
    return parsed


def parse_l3_intent(canvas: dict | None) -> dict[uuid.UUID, list[RouteSpec]]:
    """Parse every device node's ``data.l3`` in one canvas (ADR 0014 Decision 1).

    Returns a map of device id to its (deduplicated) route list, keyed only for
    devices whose node actually carries an ``l3`` key; a device with no L3 intent
    is simply absent from the map, not mapped to an empty list. Raises
    ``L3IntentMalformed`` on the first malformed node encountered (in canvas node
    order), the same failure this function's callers (the fork write paths) are
    required to have already refused via the D5 validation pass before reaching
    here, so a malformed shape should never actually surface at this call site in
    normal operation.

    Uses ``node_to_device_map`` for device id resolution, so an element or dynamic
    placeholder node (neither ever resolves to a device id) never contributes here,
    and a node whose ``data.device.id`` fails to parse as a UUID is silently
    excluded exactly as it is everywhere else that map is used.
    """
    if not canvas:
        return {}
    nodes = canvas.get("nodes") or []
    node_by_id = {node.get("id"): node for node in nodes if node.get("id") is not None}
    device_map = node_to_device_map(canvas)

    result: dict[uuid.UUID, list[RouteSpec]] = {}
    for node_id, device_id in device_map.items():
        node = node_by_id.get(node_id) or {}
        l3_data = (node.get("data") or {}).get("l3")
        if l3_data is None:
            continue
        result[device_id] = parse_node_l3(node_id, l3_data)
    return result


__all__ = [
    "L3IntentMalformed",
    "RouteSpec",
    "parse_l3_intent",
    "parse_node_l3",
]
