"""Canvas parser for Layer 3 routing intent (ADR 0014 phase 1, issue #34).

A device node whose device is a `Layer 3 Switch` may carry `data.l3 = {"routes":
[...]}` in canvas_data (ADR 0014 Decision 1). This module is the single place that
reads that shape off a canvas and turns it into ``RouteSpec`` objects; nothing else
in cabling parses ``data.l3`` directly. Imports from ``canvas_nodes`` only (R1
review fix on 2ade362c): there is no cycle to work around here.

``route_key`` is the route identity used both by the fork-save set reconcile
(``fork_save_service.save_fork``, ``fork_service.create_fork``) and by the
``fork_l3_routes`` unique constraint: it packs the same three fields execution's
``_route_run_identity`` (``services/execution/app/services/nats_consumer.py:413-424``)
already treats as a route's identity (destination, interface, next_hop), just as one
string instead of a tuple. Because the packed string uses ``|`` as its separator, any
field containing a literal ``|`` is refused as malformed (R5(d) review fix): letting
one through would make two distinct routes collide on `route_key`, or one route's
`route_key` misparse as another's.

Element and dynamic-placeholder nodes never carry ``l3``: this module only ever
looks at ``data.l3`` on nodes that ``node_to_device_map`` resolves to a real device,
so a stray ``l3`` key on a non-device node is silently never read (the same posture
``node_to_device_map`` already takes toward every other device-only field).

Two entry points parse a whole canvas, both deduplicating on ``route_key`` and both
implementing R10 (an empty ``routes: []`` on a node is no intent at all: the device
is absent from the result, not mapped to an empty list) and R5(a) (two nodes
resolving to the same device MERGE their routes rather than the second node
overwriting the first):

- ``parse_l3_intent``: strict. Raises ``L3IntentMalformed`` on the first malformed
  node encountered, in canvas node order. Used by validation and by the fork save
  path, both of which must refuse a malformed shape rather than silently drop it.
- ``parse_l3_intent_tolerant``: forgiving (R4 review fix on 2ade362c). A malformed
  node is dropped with one WARNING log naming the node, and parsing continues; used
  only by fork-on-activation (``fork_service.create_fork``), which must never gate
  the reservation on drift a topology edit introduced after the booking was already
  judged valid.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.services.canvas_nodes import node_to_device_map

logger = logging.getLogger(__name__)

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
    follow-up). ``destination`` is canonicalized (R5(c)) via
    ``ipaddress.ip_network(value, strict=False)`` when parseable, and kept verbatim
    otherwise so validation still reports ``l3_bad_destination`` for it.
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
    """Validate one field's shape and normalize an optional empty string to null.

    R5(b): an optional field submitted as ``""`` normalizes to ``None`` rather than
    being carried through as a literal empty string (a route's ``next_hop`` or
    ``virtual_router`` of ``""`` means "not set", not "set to the empty string").
    R5(d): a literal ``|`` in any field is refused as malformed, since ``route_key``
    packs its three fields with ``|`` as the separator.
    """
    if value is None:
        if required:
            raise L3IntentMalformed(node_id, f"'{field_name}' is required")
        return None
    if not isinstance(value, str):
        raise L3IntentMalformed(node_id, f"'{field_name}' must be a string")
    if "|" in value:
        raise L3IntentMalformed(node_id, f"'{field_name}' must not contain '|'")
    if required and not value:
        raise L3IntentMalformed(node_id, f"'{field_name}' must be a non-empty string")
    if len(value) > _MAX_FIELD_LENGTH:
        raise L3IntentMalformed(
            node_id, f"'{field_name}' must be at most {_MAX_FIELD_LENGTH} characters"
        )
    if not required and not value:
        return None
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
        try:
            destination = str(ipaddress.ip_network(destination, strict=False))
        except ValueError:
            pass  # kept verbatim; the validation pass reports l3_bad_destination
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


def canvas_has_l3(canvas: dict) -> bool:
    """True if any node in the canvas carries a ``data.l3`` key at all.

    A pure presence check with no parsing (never raises), so a caller can decide
    whether the L3 pass, and the extra ``resolve_canvas_wiring`` call and inventory
    round trips it needs, is worth running at all before doing any of that work
    (ADR 0014 Decision 5: a topology with no ``data.l3`` anywhere must make no
    inventory call). Over-inclusive by design: a stray ``l3`` key on a
    non-device node (never read by the parser) still makes this True, which just
    means one harmless extra ``resolve_canvas_wiring`` call in that corner case.
    """
    nodes = canvas.get("nodes") or []
    return any((node.get("data") or {}).get("l3") is not None for node in nodes)


def _collect_l3_intent(
    canvas: dict | None,
    *,
    on_malformed: Callable[[L3IntentMalformed], None],
) -> dict[uuid.UUID, list[RouteSpec]]:
    """Shared walk behind ``parse_l3_intent`` and ``parse_l3_intent_tolerant``.

    ``on_malformed`` decides what happens to a node whose ``data.l3`` fails to
    parse: raising it (strict) or logging and continuing (tolerant). Every other
    rule is the same for both callers: R5(a) two nodes resolving to the same
    device merge their routes (deduplicated on ``route_key``, first occurrence in
    canvas node order wins a collision), and R10 a node whose routes parse to an
    empty list contributes nothing (the device is absent from the result, not
    mapped to ``[]``).
    """
    if not canvas:
        return {}
    nodes = canvas.get("nodes") or []
    node_by_id = {node.get("id"): node for node in nodes if node.get("id") is not None}
    device_map = node_to_device_map(canvas)

    result: dict[uuid.UUID, list[RouteSpec]] = {}
    seen_by_device: dict[uuid.UUID, set[str]] = {}
    for node_id, device_id in device_map.items():
        node = node_by_id.get(node_id) or {}
        l3_data = (node.get("data") or {}).get("l3")
        if l3_data is None:
            continue
        try:
            routes = parse_node_l3(node_id, l3_data)
        except L3IntentMalformed as exc:
            on_malformed(exc)
            continue
        if not routes:
            continue
        bucket = result.setdefault(device_id, [])
        seen = seen_by_device.setdefault(device_id, set())
        for route in routes:
            if route.route_key in seen:
                continue
            seen.add(route.route_key)
            bucket.append(route)
    return {device_id: routes for device_id, routes in result.items() if routes}


def parse_l3_intent(canvas: dict | None) -> dict[uuid.UUID, list[RouteSpec]]:
    """Parse every device node's ``data.l3`` in one canvas (ADR 0014 Decision 1).

    Returns a map of device id to its (deduplicated, merged-across-nodes) route
    list; a device with no L3 intent is simply absent from the map. Raises
    ``L3IntentMalformed`` on the first malformed node encountered (in canvas node
    order): both fork write paths that call this directly (the validation pass,
    and the save path via ``gate_l3_intent``) must refuse a malformed shape, not
    silently drop it. Fork-on-activation uses ``parse_l3_intent_tolerant`` instead.
    """

    def _raise(exc: L3IntentMalformed) -> None:
        raise exc

    return _collect_l3_intent(canvas, on_malformed=_raise)


def parse_l3_intent_tolerant(canvas: dict | None) -> dict[uuid.UUID, list[RouteSpec]]:
    """Like ``parse_l3_intent``, but never raises (R4 review fix on 2ade362c).

    A malformed node's ``data.l3`` is dropped with one WARNING log naming the node
    and parsing continues; every well-formed node's routes are still collected.
    Used only by ``fork_service.create_fork``: activation must not gate on L3
    intent, since the reservation create already judged the topology and drift
    introduced after booking must not strand an ACTIVE reservation with no fork.
    """

    def _warn(exc: L3IntentMalformed) -> None:
        logger.warning(
            "l3_intent_dropped_at_activation node_id=%s message=%s",
            exc.node_id,
            exc.message,
        )

    return _collect_l3_intent(canvas, on_malformed=_warn)


__all__ = [
    "L3IntentMalformed",
    "RouteSpec",
    "canvas_has_l3",
    "parse_l3_intent",
    "parse_l3_intent_tolerant",
    "parse_node_l3",
]
