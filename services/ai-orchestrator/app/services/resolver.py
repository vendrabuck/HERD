"""Cabling-aware assignment of proposed roles to concrete devices.

The AI proposes a topology over TEMPLATE names: each proposed device carries a
role and a template, and each edge names two roles. Picking the first N
AVAILABLE devices of each template (the behavior this module replaces) can
resolve two roles onto devices that share no cable path at all, and the
failure only surfaces much later, when cabling's topology validator refuses
the reservation with `no_path`. This module picks devices the cabling graph
can actually connect.

The shape of the problem: each role has a candidate set (the AVAILABLE
devices of its template the caller can see), each device-to-device edge must
land on a candidate pair the cabling graph can reach, and no two roles may
take the same device. That is a small constraint-satisfaction problem, solved
here by deterministic backtracking with a hard step budget.

Everything here is a pure function over plain dicts, lists, and sets; the
HTTP calls that produce the reachability relation live in
`cabling_client.py` and the orchestration in `generator.py`. That split is
what makes the search unit-testable without a stack.

PER-PORT CAPACITY IS DELIBERATELY NOT JUDGED HERE. Which port each wire lands
on is decided later, by cabling's fork-save resolver (ADR 0006, issue #531),
which is the only place that sees the saved canvas and the live port claims;
HERD's own topology validator and fork-save path judge reachability only,
never per-port capacity. An earlier version of this module rejected a
candidate whose distinct endpoint ports (across the shortest paths the
pathfinder returned for it) fell short of its role's edge count, but that is
stricter than the system it pre-empts: a live finding against the demo
stack's real cabling (2026-09-15) showed a switch role with two edges and
every candidate reachable, all through one uplink port, which the bound
refused and HERD itself accepts as wireable. So it was removed.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

# Why the search is bounded rather than exhaustive: backtracking over R roles
# with domains of size D is O(D**R) in the worst case, and a pathological
# proposal (many same-template roles in a sparsely cabled lab) can walk a lot
# of that tree before proving infeasibility. The budget is the caller's
# `ai_resolver_max_search_steps`; one step is one candidate trial.
InfeasibleReason = Literal["no_candidate_pair", "no_consistent_assignment", "budget_exhausted"]


@dataclass(frozen=True)
class ResolverRole:
    """One proposed device role and the devices it may be resolved to.

    `candidates` is in inventory order (the order the inventory service
    listed them), which is also the order the search tries them in, so a
    repeated generation over an unchanged lab produces the same assignment.
    """

    role: str
    template_name: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class ResolverEdge:
    """One proposed edge, by role name."""

    source_role: str
    target_role: str


@dataclass(frozen=True)
class ResolverPlan:
    """The search outcome.

    `assignment` maps role to device id when the search succeeded and is None
    otherwise. On failure `unsatisfied_edges` names the edges to blame in the
    repair message, and `reason` says why the search gave up.
    """

    assignment: dict[str, str] | None = None
    unsatisfied_edges: tuple[ResolverEdge, ...] = ()
    reason: InfeasibleReason | None = None
    steps: int = 0


def pair_key(device_a: str, device_b: str) -> tuple[str, str]:
    """Order-independent key for a device pair.

    Cabling paths are undirected (the adjacency graph is built both ways), so
    reachability of (a, b) and (b, a) is the same fact and must not be stored
    or queried twice.
    """
    return (device_a, device_b) if device_a <= device_b else (device_b, device_a)


def device_edges(
    edges: Iterable[ResolverEdge],
    device_roles: Iterable[str],
) -> list[ResolverEdge]:
    """Keep only the edges whose BOTH endpoints are device roles.

    An edge touching a network element role (ADR 0012) never becomes a cable
    hop: the committer attaches it to a free port on the device side at commit
    time, and the element side has no device at all. Such an edge therefore
    constrains nothing here and is dropped before any pathfind call is made.
    """
    known = set(device_roles)
    return [e for e in edges if e.source_role in known and e.target_role in known]


def candidate_pairs(
    roles: Iterable[ResolverRole],
    edges: Iterable[ResolverEdge],
) -> list[tuple[str, str]]:
    """Every device pair the feasibility check needs, deduplicated.

    One entry per unordered pair of distinct devices drawn from the candidate
    sets of an edge's two roles. Two roles of the same template share a
    candidate set, so a same-template edge contributes the pairs among that
    set; the identity pair (a device with itself) is skipped because a role
    never resolves to the same device as its neighbour.
    """
    by_role = {r.role: r for r in roles}
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for edge in edges:
        source = by_role.get(edge.source_role)
        target = by_role.get(edge.target_role)
        if source is None or target is None:
            continue
        for device_a in source.candidates:
            for device_b in target.candidates:
                if device_a == device_b:
                    continue
                key = pair_key(device_a, device_b)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
    return pairs


def read_pathfind_results(
    results: Iterable[dict[str, Any]],
) -> set[tuple[str, str]]:
    """Turn cabling's batch pathfind results into the reachable-pair set the search needs.

    Returns the set of reachable device pairs, keyed by `pair_key`.

    A per-pair `error` is treated as NOT reachable: issue #763 gives a pair
    naming a device outside the caller's visibility the same wording an
    unknown device id gets, deliberately, so this code must not read anything
    into the distinction either.
    """
    reachable: set[tuple[str, str]] = set()
    for result in results:
        if result.get("error"):
            continue
        if not result.get("reachable"):
            continue
        source = str(result.get("source_device_id"))
        target = str(result.get("target_device_id"))
        reachable.add(pair_key(source, target))
    return reachable


def plan_assignment(
    roles: list[ResolverRole],
    edges: list[ResolverEdge],
    reachable: set[tuple[str, str]],
    *,
    max_steps: int,
) -> ResolverPlan:
    """Choose one distinct device per role so every edge is cable-reachable.

    Search order is static and most-constrained-first: fewest candidates,
    then most edges, then role name. Candidates are tried in inventory order.
    Both rules exist for determinism as much as for speed: the same lab and
    the same proposal must produce the same assignment, or a regenerate looks
    like a lab change.

    Distinctness is enforced GLOBALLY, not just within a template, which is
    the same thing (a device belongs to exactly one template, so candidate
    sets of different templates are disjoint) and cheaper to check.

    No per-port capacity bound is applied to a candidate: see the module
    docstring for why (the fork-save resolver assigns ports, not this
    module, and a bound here refused topologies HERD itself accepts).

    Worst case is O(max_steps) candidate trials, each doing O(degree) checks
    against the roles already assigned, so the whole search is hard-bounded
    by `max_steps` regardless of the proposal's shape.
    """
    device_only = device_edges(edges, (r.role for r in roles))
    degree: dict[str, int] = {r.role: 0 for r in roles}
    for edge in device_only:
        degree[edge.source_role] += 1
        degree[edge.target_role] += 1

    domains: dict[str, tuple[str, ...]] = {r.role: r.candidates for r in roles}

    # Pairwise prefilter. An edge with no feasible candidate pair at all can
    # never be satisfied by any assignment, and naming exactly those edges is
    # what makes the repair message actionable ("nothing of template A can
    # reach anything of template B"), so it is reported before the search runs.
    unsatisfiable = [
        edge for edge in device_only if not _edge_has_feasible_pair(edge, domains, reachable)
    ]
    if unsatisfiable:
        return ResolverPlan(
            assignment=None,
            unsatisfied_edges=tuple(unsatisfiable),
            reason="no_candidate_pair",
            steps=0,
        )

    neighbours: dict[str, list[str]] = {r.role: [] for r in roles}
    for edge in device_only:
        neighbours[edge.source_role].append(edge.target_role)
        neighbours[edge.target_role].append(edge.source_role)

    order = sorted(roles, key=lambda r: (len(domains[r.role]), -degree[r.role], r.role))
    assignment: dict[str, str] = {}
    used: set[str] = set()
    steps = 0
    # Deepest index the search ever assigned past, and the role that blocked
    # it there. Used only for reporting: it is the most specific honest answer
    # available when every edge is individually satisfiable but no whole
    # assignment is.
    deepest = -1
    blocked: ResolverRole | None = None
    budget_exhausted = False

    def _search(index: int) -> bool:
        nonlocal steps, deepest, blocked, budget_exhausted
        if index >= len(order):
            return True
        role = order[index]
        if index > deepest:
            deepest = index
            blocked = role
        for device in domains[role.role]:
            if steps >= max_steps:
                budget_exhausted = True
                return False
            steps += 1
            if device in used:
                continue
            if not _consistent(role.role, device, assignment, neighbours, reachable):
                continue
            assignment[role.role] = device
            used.add(device)
            if _search(index + 1):
                return True
            del assignment[role.role]
            used.discard(device)
            if budget_exhausted:
                return False
        return False

    if _search(0):
        return ResolverPlan(assignment=dict(assignment), steps=steps)

    blame = _edges_incident_to(device_only, blocked.role if blocked else None) or tuple(device_only)
    return ResolverPlan(
        assignment=None,
        unsatisfied_edges=blame,
        reason="budget_exhausted" if budget_exhausted else "no_consistent_assignment",
        steps=steps,
    )


def _edge_has_feasible_pair(
    edge: ResolverEdge,
    domains: dict[str, tuple[str, ...]],
    reachable: set[tuple[str, str]],
) -> bool:
    for device_a in domains.get(edge.source_role, ()):
        for device_b in domains.get(edge.target_role, ()):
            if device_a == device_b:
                continue
            if pair_key(device_a, device_b) in reachable:
                return True
    return False


def _consistent(
    role: str,
    device: str,
    assignment: dict[str, str],
    neighbours: dict[str, list[str]],
    reachable: set[tuple[str, str]],
) -> bool:
    for other_role in neighbours.get(role, ()):
        other_device = assignment.get(other_role)
        if other_device is None:
            continue
        if other_device == device:
            return False
        if pair_key(device, other_device) not in reachable:
            return False
    return True


def _edges_incident_to(edges: list[ResolverEdge], role: str | None) -> tuple[ResolverEdge, ...]:
    if role is None:
        return ()
    return tuple(e for e in edges if role in (e.source_role, e.target_role))
