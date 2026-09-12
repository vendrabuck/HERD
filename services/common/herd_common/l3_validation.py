"""The one per-route Layer 3 validation pass, shared by cabling and execution.

ADR 0014 addendum X-I (issue #755). Before this module there were two copies of
the same evaluation order and reason vocabulary: cabling's
``l3_validation._validate_one_route`` (the save gate and the validate endpoint)
and execution's ``nats_consumer._validate_route_at_drive_time`` (the X-A
drive-time re-validation). They were kept byte-identical by hand, which is
exactly the arrangement the VRF reasons below would have broken: a rule that
must refuse a route at the save gate AND refuse it again at drive time cannot
live in two hand-synced copies.

Both callers now import from here. The module is deliberately dependency-free
(stdlib ``ipaddress`` only) and knows nothing about cabling's ``RouteSpec``,
execution's route dicts, inventory, or HTTP: each caller unpacks its own shape
into the plain arguments below. That is what lets it live in ``herd_common``
without dragging a service's models across the service boundary.

Reason vocabulary and evaluation order (ADR 0014 Decision 5, the phase 1
amendment, and X-I), first match wins:

1. ``l3_bad_destination``: ``destination`` is not a parseable IP prefix.
2. ``l3_bad_next_hop``: ``next_hop`` is present but not a parseable address.
3. ``l3_unknown_interface``: ``interface`` is not among the config's interface
   names.
4. ``l3_unknown_virtual_router`` (X-I): the route names a VRF the config's
   ``virtual_routers`` does not declare. A config with NO ``virtual_routers``
   key declares none, so every VRF-naming route refuses here.
5. ``l3_interface_outside_virtual_router`` (X-I): the route names a declared
   VRF, but its interface is not one of that VRF's ``interfaces``.
6. ``l3_interface_bound_to_virtual_router`` (X-I): the route names NO VRF, but
   its interface is listed under some VRF. An interface enslaved to a VRF is
   not in the default routing table on Linux and FRR, so a default-table route
   through it can never install.
7. ``l3_next_hop_unverifiable`` / ``l3_next_hop_outside_interface``: the
   next-hop-versus-interface-subnet checks, unchanged.

The three VRF reasons sit AFTER ``l3_unknown_interface`` and BEFORE the
next-hop-subnet checks. That placement is behavior-preserving for every config
written before X-I: with no ``virtual_routers`` key the VRF map is empty, so
steps 4 to 6 cannot fire for a route that names no VRF, and a route that names
one could never have been drivable anyway.
"""

from __future__ import annotations

import ipaddress


def usable_interfaces(raw_interfaces: object) -> dict[str, str | None]:
    """Extract a name-to-ip map from a config's ``interfaces`` list.

    Tolerates a driver-published schema that stores any shape there: a non-list,
    or a non-dict/no-name entry, is skipped rather than raising, so a garbled
    config reports ``l3_switch_unconfigured`` (no usable interface names) at the
    caller instead of a 500 or a mid-reconcile exception.
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


def usable_virtual_routers(raw_virtual_routers: object) -> dict[str, set[str]]:
    """Extract a VRF-name-to-member-interface-names map from a config's
    ``virtual_routers`` list (ADR 0014 addendum X-I).

    Same tolerance as ``usable_interfaces``: a non-list, a non-dict entry, an
    entry with no ``name``, or a non-list ``interfaces`` value contributes
    nothing rather than raising. A config with no ``virtual_routers`` key
    therefore yields ``{}``, which is not a special case anywhere below: it
    simply means the switch declares no VRF, so every route naming one is
    ``l3_unknown_virtual_router`` and no route can be interface-bound.
    """
    if not isinstance(raw_virtual_routers, list):
        return {}
    result: dict[str, set[str]] = {}
    for entry in raw_virtual_routers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        members = entry.get("interfaces")
        names: set[str] = set()
        if isinstance(members, list):
            names = {m for m in members if isinstance(m, str) and m}
        result.setdefault(name, set()).update(names)
    return result


def validate_one_route(
    destination: object,
    next_hop: object,
    interface: object,
    virtual_router: object = None,
    *,
    interfaces: dict[str, str | None],
    virtual_routers: dict[str, set[str]] | None = None,
) -> str | None:
    """Evaluate one route's per-route reasons in order; return the first that
    applies, or ``None`` when the route is clean.

    ``interfaces`` and ``virtual_routers`` come from ``usable_interfaces`` and
    ``usable_virtual_routers`` over the switch's latest config. Interface routes
    (``next_hop`` is None) skip every next-hop check. ``next_hop`` is parsed to
    an ``ip_address`` once and reused for both the shape check and the
    interface-membership check.

    Arguments are typed ``object`` on purpose: execution drives this from plain
    event dicts where a field can be missing or any JSON type, and a malformed
    value must produce the matching reason, never a ``TypeError``.
    """
    vrfs = virtual_routers or {}

    try:
        ipaddress.ip_network(destination, strict=False)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return "l3_bad_destination"

    next_hop_addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if next_hop is not None:
        try:
            next_hop_addr = ipaddress.ip_address(next_hop)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return "l3_bad_next_hop"

    if interface not in interfaces:
        return "l3_unknown_interface"

    if virtual_router:
        # A non-string VRF name can never match a declared one, and feeding it to
        # dict.get would raise TypeError on an unhashable value.
        members = vrfs.get(virtual_router) if isinstance(virtual_router, str) else None
        if members is None:
            return "l3_unknown_virtual_router"
        if interface not in members:
            return "l3_interface_outside_virtual_router"
    else:
        for members in vrfs.values():
            if interface in members:
                return "l3_interface_bound_to_virtual_router"

    if next_hop_addr is None:
        return None

    ip_value = interfaces.get(interface)  # type: ignore[arg-type]
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


__all__ = [
    "usable_interfaces",
    "usable_virtual_routers",
    "validate_one_route",
]
