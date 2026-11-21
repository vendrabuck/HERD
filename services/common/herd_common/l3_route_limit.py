"""The most distinct static routes routing intent may give one Layer 3 switch, and one
provision may configure on it, per reservation pin (issue #1493).

It bounds what a drive configures, not what a switch holds: a pin stored before the
limit keeps its routes, a FAILED pin's record of possibly installed routes can grow
past it, and inventory's config schema and config-apply jobs do not apply it.

ONE limit, shared by definition between cabling and execution. Cabling refuses
routing intent that gives a switch more than ``MAX_ROUTES_PER_SWITCH`` distinct
routes before anything is stored (``l3_intent.walk_l3_nodes``); execution
refuses, before any driver call, a route set over the same limit that reaches it
anyway (an intent stored before the limit existed, a switch's configured routes,
a pin reused verbatim). The cabling pre-check must equal execution's judge, never
stricter or looser, so both compare through ``route_limit_exceeded`` here and
neither keeps a copy of the number.

A constant, not a setting, deliberately: a setting is read from each service's
own environment, so two services could be configured with different values and
cabling would then admit intent execution refuses (or refuse intent execution
would drive). The number also fixes how long an L3 pin's drive claim can last
(``execution``'s ``l3_pin_claim_budget``, WIRE-CLAIM-3); changing it is a code
change reviewed together with that arithmetic.

What is counted: DISTINCT routes, by the shared four-field identity
(``l3_route_identity.route_identity_key``), so a duplicate never counts twice;
and the route SET a switch is to carry, merged across every canvas node that
names the switch. The removals of what an earlier failed pass may have left on
the switch (the residue a provision removes first, WIRE-L3-19) are not counted:
refusing them would leave those routes installed, and cabling cannot see them, so
counting them would make execution's judge differ from cabling's pre-check.

The owner chose 1,000 (issue #1493): realistic lab pins carry tens to low
hundreds of routes, and at the default timeouts a drive of 1,000 route
operations holds its claim for about a day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from herd_common.l3_route_identity import route_identity_key

MAX_ROUTES_PER_SWITCH: Final = 1000


def route_limit_exceeded(distinct_route_count: int) -> bool:
    """True when a switch carrying ``distinct_route_count`` distinct routes is over
    the limit: the ONE comparison both services make."""
    return distinct_route_count > MAX_ROUTES_PER_SWITCH


def distinct_route_count(routes: Iterable[Mapping]) -> int:
    """How many distinct routes a list of route dicts names, by the shared identity
    key (a duplicate, including one that differs only by an unset optional field
    written as ``""`` instead of null, counts once)."""
    return len(
        {
            route_identity_key(
                route.get("destination"),
                route.get("interface"),
                route.get("next_hop"),
                route.get("virtual_router"),
            )
            for route in routes
        }
    )


__all__ = ["MAX_ROUTES_PER_SWITCH", "distinct_route_count", "route_limit_exceeded"]
