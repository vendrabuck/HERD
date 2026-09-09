"""Layer 3 route identity packing, shared between cabling and execution.

ADR 0014 (Decision 1, S4 review fix) and its 2026-09-09 addendum X-E (issue
#757): a route's reconcile/uniqueness identity is all four of its fields
(destination, interface, next_hop, virtual_router), JSON-packed so any field's
content is safe to include (no unescaped separator to collide with, unlike the
retired ``|``-joined scheme). Two routes differing only by ``virtual_router``
are distinct identities (ADR 0014 phase 1 round-2 review fix S4): the same
destination/interface routed via two different VRFs must coexist as separate
``fork_l3_routes`` rows.

``next_hop`` and ``virtual_router`` both normalize an empty string to null
before packing, since a route submitted with ``""`` for either means "not
set", not "set to the empty string" (ADR 0014 round-1 review fix R5(b)).
``destination`` is canonicalized via ``ipaddress.ip_network(value,
strict=False)`` when it parses as an IP network, and kept verbatim otherwise,
so a malformed destination still produces a stable (if not meaningful) key
rather than raising: validation, not identity packing, is what refuses a bad
destination.

Cabling's ``RouteSpec.route_key``
(``services/cabling/app/services/l3_intent.py``) and execution's
fork-route-to-pinned-route comparisons (ADR 0014 phase 3, issue #34) both call
``route_identity_key`` so a route's identity is computed in exactly one place,
byte-identical everywhere it is stored or compared.
"""

from __future__ import annotations

import ipaddress
import json


def route_identity_key(
    destination: str,
    interface: str,
    next_hop: str | None,
    virtual_router: str | None,
) -> str:
    """Pack one route's four identity-bearing fields into a stable string.

    Keys produced here are byte-identical to what cabling's
    ``fork_l3_routes.route_key`` column stores:
    ``json.dumps([destination, interface, next_hop or "", virtual_router or ""],
    ensure_ascii=False)``, with ``destination`` canonicalized first (a no-op
    when the caller already canonicalized it, as cabling's parser does before
    constructing a ``RouteSpec``).
    """
    try:
        destination = str(ipaddress.ip_network(destination, strict=False))
    except (ValueError, TypeError):
        pass  # kept verbatim; identity packing never refuses a bad destination
    return json.dumps(
        [destination, interface, next_hop or "", virtual_router or ""],
        ensure_ascii=False,
    )


__all__ = ["route_identity_key"]
