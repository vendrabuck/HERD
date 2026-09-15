"""Unit tests for the pure cabling-aware assignment search.

No HTTP, no stack: the resolver is plain functions over dicts, lists and
sets, and these tests pin the rules the generator depends on.
"""

from app.services.resolver import (
    ResolverEdge,
    ResolverRole,
    candidate_pairs,
    device_edges,
    pair_key,
    plan_assignment,
    read_pathfind_results,
)


def _role(role: str, template: str, *candidates: str) -> ResolverRole:
    return ResolverRole(role=role, template_name=template, candidates=tuple(candidates))


def _reachable(*pairs: tuple[str, str]) -> set[tuple[str, str]]:
    return {pair_key(a, b) for a, b in pairs}


def test_single_feasible_assignment_is_found():
    roles = [_role("a", "T1", "d1"), _role("b", "T2", "d2")]
    edges = [ResolverEdge("a", "b")]
    plan = plan_assignment(roles, edges, _reachable(("d1", "d2")), max_steps=100)
    assert plan.assignment == {"a": "d1", "b": "d2"}
    assert plan.unsatisfied_edges == ()
    assert plan.reason is None


def test_backtracking_finds_the_reachable_pair_first_n_would_miss():
    """The first candidate of each role is NOT cabled to the other role's first.

    This is the exact failure the cabling-aware resolver exists to prevent:
    positional slotting takes (d1, e1), which has no path.
    """
    roles = [_role("a", "T1", "d1", "d2"), _role("b", "T2", "e1", "e2")]
    edges = [ResolverEdge("a", "b")]
    plan = plan_assignment(roles, edges, _reachable(("d2", "e2")), max_steps=1000)
    assert plan.assignment == {"a": "d2", "b": "e2"}


def test_two_roles_of_one_template_get_distinct_devices():
    roles = [_role("a", "T1", "d1", "d2"), _role("b", "T1", "d1", "d2")]
    edges = [ResolverEdge("a", "b")]
    # d1 is reachable from itself in the relation only as a degenerate pair;
    # the real constraint here is that one device cannot serve both roles.
    plan = plan_assignment(roles, edges, _reachable(("d1", "d2")), max_steps=1000)
    assert plan.assignment is not None
    assert plan.assignment["a"] != plan.assignment["b"]
    assert set(plan.assignment.values()) == {"d1", "d2"}


def test_hub_role_with_multiple_edges_may_reuse_one_reachable_device():
    """A hub role with three edges is not penalized for reaching every
    neighbor through what would be a single uplink port at the cabling
    layer: per-port capacity is judged by the fork-save resolver, not here
    (a live finding, 2026-09-15, showed a real switch role rejected this way
    even though HERD's own validator accepts it as wireable).
    """
    roles = [
        _role("hub", "T1", "hub1"),
        _role("x", "T2", "x1"),
        _role("y", "T2", "y1"),
        _role("z", "T2", "z1"),
    ]
    edges = [ResolverEdge("hub", "x"), ResolverEdge("hub", "y"), ResolverEdge("hub", "z")]
    reachable = _reachable(("hub1", "x1"), ("hub1", "y1"), ("hub1", "z1"))
    plan = plan_assignment(roles, edges, reachable, max_steps=1000)
    assert plan.assignment == {"hub": "hub1", "x": "x1", "y": "y1", "z": "z1"}


def test_unreachable_edge_is_reported_with_no_candidate_pair():
    roles = [_role("a", "T1", "d1", "d2"), _role("b", "T2", "e1", "e2")]
    edges = [ResolverEdge("a", "b")]
    plan = plan_assignment(roles, edges, set(), max_steps=1000)
    assert plan.assignment is None
    assert plan.reason == "no_candidate_pair"
    assert [(e.source_role, e.target_role) for e in plan.unsatisfied_edges] == [("a", "b")]


def test_step_budget_exhaustion_reports_the_unsatisfied_edges():
    """Every edge is pairwise satisfiable, but no whole assignment exists.

    Three roles drawn from a two-device pool cannot all take distinct
    devices, so the search explores and fails; a budget of one trial forces
    the budget_exhausted path, which still names edges to blame.
    """
    roles = [
        _role("a", "T1", "d1", "d2"),
        _role("b", "T1", "d1", "d2"),
        _role("c", "T1", "d1", "d2"),
    ]
    edges = [ResolverEdge("a", "b"), ResolverEdge("b", "c")]
    reachable = _reachable(("d1", "d2"))

    exhausted = plan_assignment(roles, edges, reachable, max_steps=1)
    assert exhausted.assignment is None
    assert exhausted.reason == "budget_exhausted"
    assert exhausted.steps <= 1
    assert exhausted.unsatisfied_edges

    # With a real budget the same input is proved infeasible instead.
    searched = plan_assignment(roles, edges, reachable, max_steps=1000)
    assert searched.assignment is None
    assert searched.reason == "no_consistent_assignment"
    assert searched.unsatisfied_edges


def test_search_is_deterministic_for_the_same_input():
    roles = [
        _role("a", "T1", "d1", "d2", "d3"),
        _role("b", "T2", "e1", "e2", "e3"),
        _role("c", "T2", "e1", "e2", "e3"),
    ]
    edges = [ResolverEdge("a", "b"), ResolverEdge("a", "c")]
    reachable = _reachable(("d2", "e2"), ("d2", "e3"), ("d3", "e1"), ("d3", "e2"), ("d1", "e3"))
    first = plan_assignment(roles, edges, reachable, max_steps=10000)
    for _ in range(5):
        again = plan_assignment(roles, edges, reachable, max_steps=10000)
        assert again.assignment == first.assignment
        assert again.steps == first.steps


def test_element_role_edges_are_ignored():
    """An edge to a network element never becomes a hop, so it constrains nothing."""
    roles = [_role("a", "T1", "d1"), _role("b", "T2", "e1")]
    edges = [
        ResolverEdge("a", "vlan-10"),
        ResolverEdge("b", "vlan-10"),
        ResolverEdge("a", "b"),
    ]
    assert [(e.source_role, e.target_role) for e in device_edges(edges, ["a", "b"])] == [("a", "b")]
    plan = plan_assignment(roles, edges, _reachable(("d1", "e1")), max_steps=100)
    assert plan.assignment == {"a": "d1", "b": "e1"}


def test_candidate_pairs_are_deduplicated_and_order_independent():
    roles = [_role("a", "T1", "d1", "d2"), _role("b", "T1", "d1", "d2")]
    edges = [ResolverEdge("a", "b"), ResolverEdge("b", "a")]
    pairs = candidate_pairs(roles, edges)
    # Only (d1, d2) survives: the identity pair is skipped and the reversed
    # edge contributes nothing new.
    assert pairs == [("d1", "d2")]


def test_read_pathfind_results_extracts_reachability():
    results = [
        {
            "source_device_id": "d1",
            "target_device_id": "e1",
            "reachable": True,
            "hop_count": 3,
            "paths": [
                [
                    {"device_id": "d1", "port_in": None, "port_out": "ge-0/0/1"},
                    {"device_id": None, "port_in": None, "port_out": None, "hidden": True},
                    {"device_id": "e1", "port_in": "ge-0/0/9", "port_out": None},
                ],
            ],
            "error": None,
        },
        {
            "source_device_id": "d2",
            "target_device_id": "e1",
            "reachable": False,
            "hop_count": 0,
            "paths": [],
            "error": None,
        },
        {
            # An invisible endpoint answers the same way an unknown id does;
            # it must read as "no path", never as a distinguishable refusal.
            "source_device_id": "d3",
            "target_device_id": "e1",
            "reachable": False,
            "hop_count": 0,
            "paths": [],
            "error": "Device not found",
        },
    ]
    reachable = read_pathfind_results(results)
    assert reachable == {pair_key("d1", "e1")}
