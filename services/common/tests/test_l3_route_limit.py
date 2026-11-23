"""Unit tests for the shared per-switch route limit (issue #1493).

Cabling's pre-check and execution's drive gate both judge through
``route_limit_exceeded`` and count with the shared identity key, so the boundary
and the counting rule are pinned once, here.
"""

from herd_common.l3_route_limit import (
    MAX_ROUTES_PER_SWITCH,
    distinct_route_count,
    route_limit_exceeded,
)


def _route(i: int, **overrides) -> dict:
    route = {"destination": f"10.{i // 256}.{i % 256}.0/24", "interface": "eth0"}
    route.update(overrides)
    return route


def test_the_limit_is_the_owners_one_thousand():
    assert MAX_ROUTES_PER_SWITCH == 1000


def test_at_the_limit_is_allowed_and_one_over_is_refused():
    assert not route_limit_exceeded(MAX_ROUTES_PER_SWITCH)
    assert route_limit_exceeded(MAX_ROUTES_PER_SWITCH + 1)
    assert not route_limit_exceeded(MAX_ROUTES_PER_SWITCH - 1)
    assert not route_limit_exceeded(0)


def test_distinct_route_count_counts_a_duplicate_once():
    routes = [_route(i) for i in range(5)]
    assert distinct_route_count(routes + routes[:3]) == 5


def test_distinct_route_count_uses_the_shared_identity_not_raw_dicts():
    """The same route spelled differently (an unset optional field as "" or absent,
    a destination in a non-canonical spelling) is one identity, exactly as
    route_identity_key packs it; a route differing only by virtual_router is not."""
    base = {"destination": "10.0.0.0/24", "interface": "eth0", "next_hop": None}
    same = [
        base,
        {"destination": "10.0.0.0/24", "interface": "eth0"},
        {"destination": "10.0.0.0/24", "interface": "eth0", "next_hop": ""},
        {"destination": "10.0.0.7/24", "interface": "eth0", "virtual_router": ""},
    ]
    assert distinct_route_count(same) == 1
    assert distinct_route_count([base, {**base, "virtual_router": "blue"}]) == 2


def test_limit_plus_duplicates_is_still_at_the_limit():
    routes = [_route(i) for i in range(MAX_ROUTES_PER_SWITCH)]
    count = distinct_route_count(routes + routes)
    assert count == MAX_ROUTES_PER_SWITCH
    assert not route_limit_exceeded(count)
    assert route_limit_exceeded(distinct_route_count(routes + [_route(MAX_ROUTES_PER_SWITCH)]))
