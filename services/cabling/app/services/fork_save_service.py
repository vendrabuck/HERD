"""Fork save-reconcile: the release-before-build set arithmetic (issue #25 P3a).

This is the heart of ADR 0006 Decision 3 and Decision 4. A save takes the fork's
submitted canvas, resolves it to an intended physical-wiring set exactly as
fork-on-activation does, diffs that set against the fork's stored ``fork_connections``
by canonical connection identity, and applies the delta release-before-build inside
one transaction that also appends a ``fork_versions`` row through
``commit_fork_with_new_version``.

Connection identity (ADR 0001, lines 233-239) is
``(device_a_id, port_a, device_b_id, port_b, layer)`` with the two endpoints
normalized to a canonical order, so an A-to-B wire and a B-to-A wire are one
connection. The set arithmetic is:

- ``to_release = old MINUS new`` (deleted first, returning capacity),
- ``to_build   = new MINUS old`` (inserted second),
- ``unchanged  = old INTERSECT new`` (left untouched).

``resolve_canvas_wiring`` (issue #531) resolves each canvas edge to a physical path
constrained to that edge's own ``data.source_port_name``/``data.target_port_name``
when the canvas carries them, so N canvas edges between the same device pair with
distinct ports resolve to N distinct hops instead of collapsing to one. It does NOT
read ``data.layer``: every resolved hop stays a hardcoded L1 ``WireSpec`` by design,
because the execution service derives L2 membership and L3 adjacency from the
recorded L1 hops (ADR 0009 option C) and filters fork rows to layer "L1"
(``_fetch_fork_intended_wires``); writing "L2"/"L3" into the row layer would make
execution drop those rows from every reconcile. The per-line layer question is
tracked separately on issue #531 and is out of scope here.

Cross-reservation port-claim enforcement (Decision 4) runs after computing
``to_build``: a physical (device, port) endpoint wired by another ACTIVE fork may
not be claimed here, and a hit fails the save with 409. Both the reconcile staging
and the port-claim query re-run inside the version-allocation retry loop (ADR open
risk 2), so a save that lost the version race recomputes against the winner's
now-committed rows rather than half-applying.

Issue #721 (2026-09-04 review sweep) found that the plain claim SELECT alone is not
enough: two concurrent saves on two different forks run at Postgres READ COMMITTED,
so neither's claim query sees the other's uncommitted inserts, textbook write skew,
and the version-allocation retry that was meant to catch the residual window only
ever fires for a same-fork conflict, which this check never flags in the first
place (its own rows are excluded). ``lock_port_claims`` closes the window: it takes
a transaction-scoped Postgres advisory lock over every claimed ``(device_id, port)``
pair, keyed exactly like reservations' ``_acquire_device_locks``, immediately before
``assert_no_port_claims`` runs. The loser blocks until the winner's transaction
commits or rolls back, so its own claim check then reads the winner's committed
rows and 409s correctly instead of racing past it. Both helpers are shared with
``fork_service.create_fork``, whose activation-time snapshot never ran this check at
all (ADR 0006 amendment): the same collision was reachable there with zero
concurrency, since two reservations forking the same topology simply both
materialize the same hops with nothing to object.

ADR 0014 phase 1 (issue #34), restructured by the R1-R3 review fixes on 2ade362c:
``node_to_device_map``, ``node_to_element_map``, and ``classify_element_edge`` now
live in the leaf module ``canvas_nodes.py`` and are re-exported here for existing
importers. ``gate_l3_intent`` no longer resolves wiring or parses intent itself
(R3): the caller (``routes/forks.py``'s ``save_fork_internal``) resolves the canvas
via ``resolve_canvas_wiring`` and calls ``gate_l3_intent`` with that resolution
BEFORE taking the fork row's ``FOR UPDATE`` lock, so the gate's inventory HTTP
calls never run while any lock is held; ``save_fork`` itself now takes the already
-resolved wiring and already-parsed (and already-gated) intent as parameters,
so the version-allocation retry loop's reapply covers only the set arithmetic,
never a second resolve or a second gate call.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Callable, TypeVar

from fastapi import HTTPException
from herd_common import advisory_lock
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.fork import (
    ForkConnection,
    ForkL3Route,
    ForkStatus_ACTIVE,
    ForkVersion,
    ReservationFork,
)
from app.services.canvas_nodes import classify_element_edge, node_to_device_map, node_to_element_map
from app.services.l3_intent import L3IntentMalformed, RouteSpec, parse_l3_intent
from app.services.l3_validation import validate_canvas_l3
from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths_async
from app.services.version_service import commit_fork_with_new_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WireSpec:
    """One resolved physical hop of the intended wiring, before it becomes a row."""

    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    layer: str
    physical_connection_id: uuid.UUID | None = None
    # The React Flow canvas edge id this hop was resolved from (issue #345 P3b).
    # Carried through resolve to persistence so the execution consumer can group the
    # hops of one canvas edge. Deliberately absent from the identity helpers below, so
    # a re-save that changes only the edge id reconciles as unchanged, not release+build.
    edge_key: str | None = None


@dataclass
class ForkSaveResult:
    """The reconcile outcome the endpoint returns (ADR 0006 Decision 2 contract)."""

    fork_id: uuid.UUID
    version_number: int
    released: list[WireSpec]
    built: list[WireSpec]
    unchanged_count: int
    # ADR 0012 phase 1 (issue #22): count of device-to-element attachment edges the
    # resolver recognized and skipped explicitly. Additive; defaults to 0 so every
    # other ForkSaveResult construction site (prune, version-race retries) is
    # unaffected.
    element_attachments_skipped: int = 0
    # ADR 0014 phase 1 (issue #34): counts from the same save's Layer 3
    # routing-intent set reconcile (fork_l3_routes). Additive, default 0.
    l3_routes_built: int = 0
    l3_routes_released: int = 0


@dataclass
class ForkPruneResult:
    """The device-prune outcome (ADR 0009 Decision 6 REMOVE half, issue #459).

    ``changed`` is True iff wiring or Layer 3 routing intent (ADR 0014 phase 1,
    issue #34) was released and a fork_versions row appended; a no-op replay
    (nothing left to release on either front) returns the current latest version
    with ``changed`` False so the caller stages nothing.
    """

    fork_id: uuid.UUID
    version_number: int
    changed: bool
    released: list[WireSpec]


def assert_endpoints_are_members(canvas: dict | None, member_device_ids: set[uuid.UUID]) -> None:
    """Refuse a canvas whose endpoint devices fall outside the reservation (D2, the
    2026-09-04 sweep's fork endpoint-membership finding).

    Checks ``node_to_device_map(canvas).values()`` ONLY: a network element node is
    not a device (``node_to_device_map`` never includes one) and a resolved path's
    transit devices are never checked here, since transit gear on a resolved path is
    legitimately non-member. A node whose device id does not parse as a UUID is
    already dropped by ``node_to_device_map``, so it cannot hide a violation. Raises
    409 with a stable, machine-readable detail naming every offending device so the
    caller can PATCH-add or remove it. Admins are not exempt.
    """
    if not canvas:
        return
    offending = {
        device_id
        for device_id in node_to_device_map(canvas).values()
        if device_id not in member_device_ids
    }
    if offending:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "fork_device_not_member",
                "device_ids": sorted(str(device_id) for device_id in offending),
            },
        )


@dataclass(frozen=True)
class CanvasWiringResolution:
    """The result of resolving a canvas's committed edges (ADR 0012 phase 1).

    ``specs`` is unchanged from what ``resolve_canvas_wiring`` returned before this
    phase. ``element_attachments_skipped`` counts the device-to-element edges the
    resolver recognized and skipped explicitly: a network element edge never becomes a
    hop (decision 2), so it contributes no WireSpec, but the skip is now counted rather
    than falling through the generic unresolvable-endpoint branch indistinguishably
    from a genuinely broken edge.
    """

    specs: list[WireSpec]
    element_attachments_skipped: int = 0


async def resolve_canvas_wiring(db: AsyncSession, canvas: dict | None) -> CanvasWiringResolution:
    """Resolve a canvas's committed edges to intended physical wiring (WireSpecs).

    The shared resolver behind both fork-on-activation snapshotting and save-reconcile
    (issue #25 P3a). For each committed (non-proposal) canvas edge between two
    resolvable devices it picks a shortest physical path and records every hop as an L1
    WireSpec carrying its backing physical connection id. Multi-hop paths (an
    off-canvas patch panel between the endpoints) yield one WireSpec per cable, and two
    edges sharing a hop de-duplicate on the path-orientation key so the same cable is
    not emitted twice. Save-time normalization (see connection_identity) collapses any
    remaining opposite-orientation duplicates.

    Port-aware resolution (issue #531): when an edge carries
    ``data.source_port_name``/``data.target_port_name`` (the multi-port wiring dialog,
    PR #530; empty string is treated as absent), the path search is constrained to
    leave the source device on that exact port and arrive at the target device on that
    exact port, so N canvas edges between the same device pair with distinct ports
    resolve to N distinct hop sets instead of the second-and-later edges hitting
    ``seen`` and contributing nothing. An edge with no port data keeps today's
    unconstrained device-pair search (older exports, bulk import, the /api/v1 facade).
    An edge whose constrained search finds no path contributes nothing for that edge
    and is logged at INFO; it NEVER falls back to the unconstrained device-pair search,
    since a fallback would silently wire different ports than the user chose. Layer is
    deliberately not read from the canvas here; see the module docstring.

    Network element edges (ADR 0012 phase 1, issue #22): every edge is classified via
    the shared ``classify_element_edge`` helper BEFORE the generic unresolvable-endpoint
    check below, so only the edges ``validate_canvas_edges`` would accept as a valid
    attachment (classification ``"attachment"``) are counted in the returned
    ``element_attachments_skipped``. An element edge the validator would reject
    (``"element_to_element"`` or ``"element_edge_no_port"``) falls through to the
    existing silent ``continue``, uncounted, same as a genuinely broken non-element
    edge. An element edge never becomes a hop (decision 2): none of the three element
    classifications contribute a WireSpec.
    """
    if not canvas:
        return CanvasWiringResolution(specs=[])
    edges = canvas.get("edges") or []
    if not edges:
        return CanvasWiringResolution(specs=[])

    node_to_device = node_to_device_map(canvas)
    node_to_element = node_to_element_map(canvas)
    graph = await build_adjacency_graph(db, device_ids=set(node_to_device.values()))

    component_devices = set(graph.keys())
    if component_devices:
        phys_rows = (
            await db.execute(
                select(
                    Connection.id,
                    Connection.device_a_id,
                    Connection.port_a,
                    Connection.device_b_id,
                    Connection.port_b,
                ).where(
                    or_(
                        Connection.device_a_id.in_(component_devices),
                        Connection.device_b_id.in_(component_devices),
                    )
                )
            )
        ).all()
    else:
        phys_rows = []
    phys_index: dict[tuple[uuid.UUID, str, uuid.UUID, str], uuid.UUID] = {}
    for conn_id, da, pa, db_dev, pb in phys_rows:
        phys_index[(da, pa, db_dev, pb)] = conn_id
        phys_index[(db_dev, pb, da, pa)] = conn_id

    specs: list[WireSpec] = []
    seen: set[tuple[uuid.UUID, str, uuid.UUID, str, str]] = set()
    element_attachments_skipped = 0

    for edge in edges:
        edge_data = edge.get("data") or {}
        if edge_data.get("isProposal"):
            continue
        edge_source = edge.get("source")
        edge_target = edge.get("target")
        classification = classify_element_edge(edge, node_to_device, node_to_element)
        if classification == "attachment":
            element_attachments_skipped += 1
            continue
        if classification in ("element_to_element", "element_edge_no_port"):
            # Not a shape the validator accepts either; fall through to the same
            # silent skip a genuinely broken non-element edge takes, uncounted.
            continue
        source_device = node_to_device.get(edge_source)
        target_device = node_to_device.get(edge_target)
        if source_device is None or target_device is None:
            continue

        # The canvas edge id, carried onto every hop this edge resolves to so the
        # consumer can group them (issue #345 P3b). Coerced to str for the String(255)
        # column; a missing id leaves the hops ungrouped (NULL). A hop shared by two
        # edges de-duplicates on the path-orientation ``seen`` key below, so it keeps
        # the edge id of whichever edge claimed it first.
        raw_edge_id = edge.get("id")
        edge_key = str(raw_edge_id) if raw_edge_id is not None else None

        # Per-edge port constraints (issue #531). A blank string from the canvas is
        # treated as absent, same as a missing key.
        raw_source_port = edge_data.get("source_port_name")
        raw_target_port = edge_data.get("target_port_name")
        source_port = str(raw_source_port) if raw_source_port else None
        target_port = str(raw_target_port) if raw_target_port else None

        paths = await find_all_shortest_paths_async(
            graph,
            source_device,
            target_device,
            source_port=source_port,
            target_port=target_port,
        )
        if not paths:
            if source_port is not None or target_port is not None:
                logger.info(
                    "resolve_canvas_wiring: unresolvable port-constrained edge "
                    "edge_id=%s source_device=%s source_port=%s target_device=%s "
                    "target_port=%s",
                    edge.get("id"),
                    source_device,
                    source_port,
                    target_device,
                    target_port,
                )
            continue

        path = paths[0]
        for first, second in zip(path, path[1:]):
            da = first.device_id
            pa = first.port_out
            db_dev = second.device_id
            pb = second.port_in
            if pa is None or pb is None:
                continue
            key = (da, pa, db_dev, pb, "L1")
            if key in seen:
                continue
            seen.add(key)
            specs.append(
                WireSpec(
                    device_a_id=da,
                    port_a=pa,
                    device_b_id=db_dev,
                    port_b=pb,
                    layer="L1",
                    physical_connection_id=phys_index.get((da, pa, db_dev, pb)),
                    edge_key=edge_key,
                )
            )

    if element_attachments_skipped:
        logger.debug(
            "resolve_canvas_wiring: skipped %d network element attachment edge(s), "
            "no hop emitted for any of them",
            element_attachments_skipped,
        )
    return CanvasWiringResolution(
        specs=specs, element_attachments_skipped=element_attachments_skipped
    )


def connection_identity(
    device_a_id: uuid.UUID,
    port_a: str,
    device_b_id: uuid.UUID,
    port_b: str,
    layer: str,
) -> tuple[str, str, str, str, str]:
    """Canonical identity of a connection (ADR 0001 lines 233-239).

    The two ``(device, port)`` endpoints are sorted to a canonical order so that a
    wire and its reverse collapse to one identity, then combined with the layer. Two
    connections are the same iff their identities are equal; ``physical_connection_id``
    is deliberately excluded (a wire re-resolved over a different physical row is still
    the same lease wire).
    """
    a = (str(device_a_id), port_a)
    b = (str(device_b_id), port_b)
    lo, hi = sorted((a, b))
    return (lo[0], lo[1], hi[0], hi[1], layer)


def _row_identity(row: ForkConnection) -> tuple[str, str, str, str, str]:
    return connection_identity(row.device_a_id, row.port_a, row.device_b_id, row.port_b, row.layer)


def _spec_identity(spec: WireSpec) -> tuple[str, str, str, str, str]:
    return connection_identity(
        spec.device_a_id, spec.port_a, spec.device_b_id, spec.port_b, spec.layer
    )


_K = TypeVar("_K")
_Old = TypeVar("_Old")
_New = TypeVar("_New")


def reconcile_by_identity(
    old_rows: list[_Old],
    new_items: list[_New],
    old_key: Callable[[_Old], _K],
    new_key: Callable[[_New], _K],
) -> tuple[list[_Old], list[_New], int]:
    """Generic release-before-build set arithmetic keyed by an arbitrary identity
    (R8 review fix on 2ade362c): both ``reconcile_connection_sets`` (wiring) and
    ``reconcile_l3_route_sets`` (routing intent) are "diff two collections by
    identity, delete what left, insert what arrived, leave the intersection
    untouched", so this is the one implementation both build on.

    Returns ``(to_release, to_build, unchanged_count)``. ``to_release`` are the old
    rows whose identity is absent from the new set (deleted first); ``to_build``
    are the new items whose identity is absent from the old set (inserted second);
    ``unchanged_count`` is the size of the intersection, left untouched. An item
    that "moves" (its identity changes while something about it stays recognizably
    the same to a caller) has its old identity in ``to_release`` and its new
    identity in ``to_build``, never an in-place mutation. Both output lists are
    sorted by the string form of their identity key, so two runs over the same
    input produce ``to_release``/``to_build`` in the same order.
    """
    old_map: dict[_K, _Old] = {old_key(row): row for row in old_rows}
    new_map: dict[_K, _New] = {new_key(item): item for item in new_items}
    old_keys = set(old_map)
    new_keys = set(new_map)
    to_release = [old_map[k] for k in sorted(old_keys - new_keys, key=str)]
    to_build = [new_map[k] for k in sorted(new_keys - old_keys, key=str)]
    unchanged_count = len(old_keys & new_keys)
    return to_release, to_build, unchanged_count


def reconcile_connection_sets(
    old_rows: list[ForkConnection],
    new_specs: list[WireSpec],
) -> tuple[list[ForkConnection], list[WireSpec], int]:
    """Pure release-before-build set arithmetic keyed by canonical identity.

    Returns ``(to_release_rows, to_build_specs, unchanged_count)``. ``to_release`` are
    the existing rows whose identity is absent from the new set (deleted first);
    ``to_build`` are the new specs whose identity is absent from the old set (inserted
    second); ``unchanged`` is the intersection, left untouched. A wire that moves
    (ports or layer) has its old identity in ``to_release`` and its new identity in
    ``to_build``, so the move is a release plus a build across the same physical port
    pair, never an in-place mutation.
    """
    return reconcile_by_identity(old_rows, new_specs, _row_identity, _spec_identity)


def touched_devices_from_specs(specs: list[WireSpec]) -> set[uuid.UUID]:
    """Both endpoints of every resolved hop (R2 review fix on 2ade362c).

    The L3 pass's "unattached" check is defined against this set, not against the
    edge-validation pass's own BFS: port constraints are honored (a port-
    constrained edge whose port has no cable resolves to no hop and so touches
    nothing) and transit devices on a multi-hop resolved path are included (they
    are never themselves an edge endpoint, but the resolved wiring genuinely
    reaches them).
    """
    touched: set[uuid.UUID] = set()
    for spec in specs:
        touched.add(spec.device_a_id)
        touched.add(spec.device_b_id)
    return touched


async def gate_l3_intent(
    db: AsyncSession,
    canvas: dict | None,
    wiring_resolution: CanvasWiringResolution,
) -> dict[uuid.UUID, list[RouteSpec]]:
    """Parse and refuse an invalid L3 intent (ADR 0014 Decision 5), returning the
    parsed intent on success so the caller never parses the same canvas twice (R8
    review fix on 2ade362c).

    Callers (``routes/forks.py``'s ``save_fork_internal``) must call this BEFORE
    taking the fork row's ``FOR UPDATE`` lock (R3): it makes inventory HTTP calls
    (a device-type batch fetch plus per-switch config-version reads) and must
    never run while any lock is held, nor repeat on a version-race retry.
    ``wiring_resolution`` must be the SAME ``resolve_canvas_wiring`` result the
    caller goes on to reconcile with (R2): this gate reuses it for the L3 pass's
    ``touched_devices`` rather than resolving the canvas a second time.

    Three outcomes:

    - ``parse_l3_intent(canvas)`` raises ``L3IntentMalformed``: refuse immediately
      with 422 ``{"error": "l3_intent_malformed", "node_id", "message"}``. No
      validation call, no inventory call: the shape is already known bad from the
      parse alone.
    - The parsed intent is empty (no device node carries ``data.l3`` at all, or
      every node's carries only an empty routes list, R10): return ``{}`` with no
      validation call and no inventory call. A fork save has never validated
      physical edge paths and must not start validating anything for a canvas
      that expresses no L3 intent either.
    - Otherwise, run ``validate_canvas_l3`` (the same L3 pass the validate routes
      run) against ``touched_devices_from_specs(wiring_resolution.specs)``, and
      refuse with 409 ``{"error": "l3_intent_invalid", "invalid_routes": [...]}``
      on any entry. A malformed shape can no longer appear in that list at this
      point, since our own parse above already proved every l3-carrying node
      parses cleanly.
    """
    try:
        intended = parse_l3_intent(canvas)
    except L3IntentMalformed as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "l3_intent_malformed",
                "node_id": exc.node_id,
                "message": exc.message,
            },
        ) from exc
    if not intended:
        return {}

    touched_devices = touched_devices_from_specs(wiring_resolution.specs)
    invalid_routes = await validate_canvas_l3(canvas or {}, db, touched_devices)
    if not invalid_routes:
        return intended
    raise HTTPException(
        status_code=409,
        detail={
            "error": "l3_intent_invalid",
            "invalid_routes": [route.model_dump() for route in invalid_routes],
        },
    )


def _l3_row_identity(row: ForkL3Route) -> tuple[uuid.UUID, str]:
    return row.device_id, row.route_key


def _l3_item_identity(item: tuple[uuid.UUID, RouteSpec]) -> tuple[uuid.UUID, str]:
    device_id, route = item
    return device_id, route.route_key


def l3_row_from_spec(
    fork_id: uuid.UUID, device_id: uuid.UUID, route: RouteSpec, created_by: str
) -> ForkL3Route:
    """Build one ``ForkL3Route`` row from a parsed route (R8 review fix on
    2ade362c): the one place both write paths (``save_fork``'s reconcile and
    ``fork_service.create_fork``'s insert loop) construct this row, so the two
    can never drift on which fields it carries."""
    return ForkL3Route(
        fork_id=fork_id,
        device_id=device_id,
        destination=route.destination,
        next_hop=route.next_hop,
        interface=route.interface,
        virtual_router=route.virtual_router,
        route_key=route.route_key,
        created_by=created_by,
    )


def reconcile_l3_route_sets(
    old_rows: list[ForkL3Route],
    intended: dict[uuid.UUID, list[RouteSpec]],
) -> tuple[list[ForkL3Route], list[tuple[uuid.UUID, RouteSpec]], int]:
    """Pure set arithmetic for the L3 route reconcile, keyed by (device_id, route_key).

    ``intended`` is ``parse_l3_intent``'s return shape. Returns ``(to_release_rows,
    to_build, unchanged_count)`` where ``to_build`` is a list of ``(device_id,
    RouteSpec)`` pairs, mirroring ``reconcile_connection_sets``'s release-before
    -build shape exactly (both now built on ``reconcile_by_identity``).
    """
    new_items: list[tuple[uuid.UUID, RouteSpec]] = [
        (device_id, route) for device_id, routes in intended.items() for route in routes
    ]
    return reconcile_by_identity(old_rows, new_items, _l3_row_identity, _l3_item_identity)


async def lock_port_claims(db: AsyncSession, to_build: list[WireSpec]) -> None:
    """Acquire transaction-scoped Postgres advisory locks over every ``to_build``
    wire's physical ``(device_id, port)`` endpoints, immediately before
    ``assert_no_port_claims`` (ADR 0006 Decision 4 amendment, issue #721).

    Closes the write-skew window a plain SELECT claim check cannot: two concurrent
    writers (two saves, or a save racing an activation snapshot) each see no
    conflict against the other's uncommitted rows under Postgres READ COMMITTED.
    The lock serializes them instead: the second caller blocks here until the first
    caller's transaction commits or rolls back, so its own claim-check SELECT then
    reads the winner's committed rows and 409s correctly. Keyed by the canonical
    string ``f"forkport:{device_id}:{port}"``, sorted before acquisition (stable
    ordering avoids deadlocks between two writers claiming overlapping port sets in
    different orders) and hashed via ``advisory_key_from_string``, the exact idiom
    reservations' ``_acquire_device_locks`` uses, so both call sites derive the same
    lock key for the same physical port. Transaction-scoped: auto-releases on the
    caller's next commit or rollback, no explicit unlock. No-op on SQLite
    (``xact_lock``'s own dialect gate; the unit suite runs in-memory with no
    advisory-lock support).
    """
    if not to_build:
        return
    keys: set[str] = set()
    for spec in to_build:
        keys.add(f"forkport:{spec.device_a_id}:{spec.port_a}")
        keys.add(f"forkport:{spec.device_b_id}:{spec.port_b}")
    for key in sorted(keys):
        await advisory_lock.xact_lock(db, advisory_lock.advisory_key_from_string(key))


async def assert_no_port_claims(
    db: AsyncSession,
    fork_id: uuid.UUID,
    to_build: list[WireSpec],
) -> None:
    """Cross-reservation port-claim enforcement (ADR 0006 Decision 4).

    A claim is a physical ``(device_id, port)`` endpoint held by another fork whose
    parent reservation is ACTIVE. If any endpoint of a ``to_build`` wire is already
    claimed, the save is refused with 409 naming every blocking reservation and port;
    the fork is left unchanged. ARCHIVED forks never block (their wiring is history,
    not a claim), and the fork's own rows are excluded. Re-run inside the version-retry
    loop so a save that lost the race sees the winner's committed claims. Callers must
    call ``lock_port_claims`` on the same ``to_build`` immediately before this (issue
    #721): the lock, not this SELECT alone, is what closes the concurrent-writer race.
    Shared with ``fork_service.create_fork``'s activation-time snapshot.
    """
    if not to_build:
        return

    claimed: set[tuple[uuid.UUID, str]] = set()
    for spec in to_build:
        claimed.add((spec.device_a_id, spec.port_a))
        claimed.add((spec.device_b_id, spec.port_b))
    claimed_devices = {device for device, _ in claimed}

    rows = (
        await db.execute(
            select(
                ForkConnection.device_a_id,
                ForkConnection.port_a,
                ForkConnection.device_b_id,
                ForkConnection.port_b,
                ReservationFork.reservation_id,
            )
            .join(ReservationFork, ForkConnection.fork_id == ReservationFork.id)
            .where(
                ReservationFork.id != fork_id,
                ReservationFork.status == ForkStatus_ACTIVE,
                or_(
                    ForkConnection.device_a_id.in_(claimed_devices),
                    ForkConnection.device_b_id.in_(claimed_devices),
                ),
            )
        )
    ).all()

    conflicts: set[tuple[str, str, str]] = set()
    for da, pa, db_dev, pb, other_reservation_id in rows:
        for device, port in ((da, pa), (db_dev, pb)):
            if (device, port) in claimed:
                conflicts.add((str(other_reservation_id), str(device), port))

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": ("One or more ports are already claimed by another active reservation"),
                "conflicts": [
                    {"reservation_id": rid, "device_id": device, "port": port}
                    for rid, device, port in sorted(conflicts)
                ],
            },
        )


async def save_fork(
    db: AsyncSession,
    fork: ReservationFork,
    canvas_data: dict,
    member_device_ids: set[uuid.UUID],
    wiring_resolution: CanvasWiringResolution,
    intended_routes: dict[uuid.UUID, list[RouteSpec]],
    created_by: str = "system",
) -> ForkSaveResult:
    """Reconcile a fork's wiring against a submitted canvas and append a version.

    Reconciles and commits under the version-allocation retry loop. The reconcile
    (endpoint-membership check, fresh old-set read, port-claim lock plus check,
    release-before-build staging) runs on the first pass here and re-runs inside
    ``commit_fork_with_new_version``'s reapply hook on every retry, so a lost
    version race recomputes against committed rows and a mid-reconcile failure
    rolls back the whole save (no half-apply, no orphan version). The caller has
    already refused an ARCHIVED fork.

    ``member_device_ids`` is the reservation's device set (D2, the 2026-09-04 fork
    endpoint-membership fix): ``assert_endpoints_are_members`` runs first inside
    ``reconcile()``, before the port-claim check, so a canvas naming a foreign device
    is refused with 409 and leaves fork_connections and fork_versions untouched.

    ADR 0014 phase 1 (issue #34), restructured by the R3 review fix on 2ade362c:
    ``wiring_resolution`` and ``intended_routes`` are computed and gated by the
    CALLER (``routes/forks.py``'s ``save_fork_internal``), BEFORE the fork row's
    ``FOR UPDATE`` lock is taken, since the gate makes inventory HTTP calls that
    must never run while any lock is held. This function receives both already
    resolved, gated, and parsed: ``reconcile()`` (and its retry reapply) only reads
    the fork's CURRENT ``ForkConnection``/``ForkL3Route`` rows fresh and reconciles
    them against these fixed inputs, so a version-race retry recomputes the delta
    against the winner's committed rows without repeating any inventory call or
    re-resolving the canvas.
    """
    # Capture the id up front: a version-race rollback expires ``fork``, and a later
    # lazy ``fork.id`` read inside the reconcile closure would attempt synchronous IO
    # off the async loop. Setting ``fork.canvas_data`` is a plain attribute write and
    # needs no load.
    fork_id = fork.id
    new_specs = wiring_resolution.specs

    result: dict = {}

    async def reconcile() -> None:
        assert_endpoints_are_members(canvas_data, member_device_ids)
        fork.canvas_data = canvas_data
        old_rows = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork_id)))
            .scalars()
            .all()
        )
        to_release, to_build, unchanged_count = reconcile_connection_sets(old_rows, new_specs)
        await lock_port_claims(db, to_build)
        await assert_no_port_claims(db, fork_id, to_build)

        # Release before build: delete and flush the freed rows first so a moved wire
        # cannot collide with its own prior row on the unique constraint.
        for row in to_release:
            await db.delete(row)
        await db.flush()
        for spec in to_build:
            db.add(
                ForkConnection(
                    fork_id=fork_id,
                    device_a_id=spec.device_a_id,
                    port_a=spec.port_a,
                    device_b_id=spec.device_b_id,
                    port_b=spec.port_b,
                    layer=spec.layer,
                    physical_connection_id=spec.physical_connection_id,
                    edge_key=spec.edge_key,
                    created_by=created_by,
                )
            )

        # Capture the deltas as plain specs now, while the released rows are still
        # readable (they vanish on commit).
        result["released"] = [
            WireSpec(
                device_a_id=row.device_a_id,
                port_a=row.port_a,
                device_b_id=row.device_b_id,
                port_b=row.port_b,
                layer=row.layer,
                physical_connection_id=row.physical_connection_id,
                edge_key=row.edge_key,
            )
            for row in to_release
        ]
        result["built"] = list(to_build)
        result["unchanged_count"] = unchanged_count

        # ADR 0014 phase 1 (issue #34): Layer 3 routing-intent set reconcile, after
        # the wiring release/build staging above. The gate already ran, and
        # ``intended_routes`` was already parsed, once, outside this closure (see
        # save_fork's docstring); this only re-reads the fork's CURRENT
        # ForkL3Route rows fresh and reconciles them, so a version-race retry
        # recomputes the delta against the winner's committed rows without
        # repeating any inventory call.
        old_l3_rows = (
            (await db.execute(select(ForkL3Route).where(ForkL3Route.fork_id == fork_id)))
            .scalars()
            .all()
        )
        l3_to_release, l3_to_build, _l3_unchanged_count = reconcile_l3_route_sets(
            old_l3_rows, intended_routes
        )
        for row in l3_to_release:
            await db.delete(row)
        await db.flush()
        for device_id, route in l3_to_build:
            db.add(l3_row_from_spec(fork_id, device_id, route, created_by))
        result["l3_routes_built"] = len(l3_to_build)
        result["l3_routes_released"] = len(l3_to_release)

    await reconcile()
    # Consume the restore-to-draft marker (issue #622): if the draft being saved was
    # last restored from an earlier version, THIS is the save that finally reconciles
    # it, so this new version is the one that carries restored_from_id, and the
    # fork-row marker is cleared in the same transaction (commit_fork_with_new_version
    # reapplies both fork.canvas_data and this clear together on a version-race
    # retry, so a retry cannot resurrect the pre-clear marker).
    restored_from_id = fork.draft_restored_from_id
    fork.draft_restored_from_id = None
    snapshot = ForkVersion(
        fork_id=fork_id, canvas_data=canvas_data, restored_from_id=restored_from_id
    )
    await commit_fork_with_new_version(db, fork, snapshot, reconcile=reconcile)

    return ForkSaveResult(
        fork_id=fork_id,
        version_number=snapshot.version_number,
        released=result["released"],
        built=result["built"],
        unchanged_count=result["unchanged_count"],
        element_attachments_skipped=wiring_resolution.element_attachments_skipped,
        l3_routes_built=result["l3_routes_built"],
        l3_routes_released=result["l3_routes_released"],
    )


def prune_canvas_for_devices(
    canvas: dict | None, removed_ids: set[str]
) -> tuple[dict | None, bool, set[str], set[str]]:
    """Remove the given devices' nodes and incident edges from a canvas.

    Returns ``(pruned_canvas, changed, remaining_edge_ids, pruned_edge_ids)``. Node
    device resolution mirrors node_to_device_map (node.data.device.id), so a node this
    helper keeps is exactly a node the save resolver would keep. The two edge-id sets
    partition the canvas's identifiable edges: ``pruned_edge_ids`` are edges incident
    to a removed device's node, ``remaining_edge_ids`` are everything else. A None or
    empty canvas prunes to itself with empty sets.
    """
    if not canvas:
        return canvas, False, set(), set()
    nodes = canvas.get("nodes") or []
    edges = canvas.get("edges") or []
    pruned_node_ids = {
        node.get("id")
        for node in nodes
        if str(((node.get("data") or {}).get("device") or {}).get("id")) in removed_ids
    }
    kept_nodes = [n for n in nodes if n.get("id") not in pruned_node_ids]
    kept_edges: list[dict] = []
    dropped_edges: list[dict] = []
    for edge in edges:
        if edge.get("source") in pruned_node_ids or edge.get("target") in pruned_node_ids:
            dropped_edges.append(edge)
        else:
            kept_edges.append(edge)
    changed = len(kept_nodes) != len(nodes) or bool(dropped_edges)
    pruned = {**canvas, "nodes": kept_nodes, "edges": kept_edges}
    remaining_edge_ids = {str(e.get("id")) for e in kept_edges if e.get("id") is not None}
    pruned_edge_ids = {str(e.get("id")) for e in dropped_edges if e.get("id") is not None}
    return pruned, changed, remaining_edge_ids, pruned_edge_ids


def _rows_released_by_prune(
    rows: list[ForkConnection],
    removed: set[str],
    remaining_edge_ids: set[str],
    pruned_edge_ids: set[str],
) -> list[ForkConnection]:
    """Select the fork_connections a device removal releases (issue #459).

    The intended set is ``fork_connections`` (the last saved wiring), never the draft
    canvas, and the edge-id sets come from the last SAVED canvas. A row releases when:

    - its edge_key belongs to a PRUNED saved edge (an edge whose endpoint device was
      removed): every hop of that edge releases, including far hops that do not touch
      the removed device (a multi-hop path's remote cable); or
    - it touches a removed device and its edge_key is NOT a REMAINING saved edge. A
      row whose edge_key IS a remaining edge is a through-hop: the removed device sits
      mid-path on an edge between devices still held, and that wiring stays. A NULL or
      stale edge_key on a row touching a removed device cannot prove a remaining edge
      is served, so it releases (the pre-#345 ungrouped rows and the loose-draft
      divergence case both land here).
    """
    released: list[ForkConnection] = []
    for row in rows:
        edge_key = str(row.edge_key) if row.edge_key is not None else None
        if edge_key is not None and edge_key in pruned_edge_ids:
            released.append(row)
            continue
        if str(row.device_a_id) not in removed and str(row.device_b_id) not in removed:
            continue
        if edge_key is None or edge_key not in remaining_edge_ids:
            released.append(row)
    return released


async def prune_fork_devices(
    db: AsyncSession,
    fork: ReservationFork,
    device_ids: list[uuid.UUID],
) -> ForkPruneResult:
    """Release removed devices' wiring from the fork's INTENDED set (issue #459).

    The ADR 0009 Decision 6 REMOVE half, redesigned to never read the draft canvas as
    wiring intent: the release is computed set-arithmetically from ``fork_connections``
    (the last saved wiring) plus the last SAVED canvas's edge incidence, so an unsaved
    draft edit can neither be built nor released by a device removal. Three effects in
    one transaction:

    - the released rows are deleted (their cross-reservation port claims free with
      them; a pure release computes no ``to_build``, so unlike a save this can never
      409 on a port claim, issue #462's deterministic trigger);
    - the stored DRAFT canvas is scrubbed of the removed devices' nodes and incident
      edges, leaving every other draft edit untouched (unsaved edges between remaining
      devices survive, un-built and un-released), so a later user save cannot rebuild
      wiring for a device the reservation no longer holds;
    - a fork_versions row is appended whose canvas is the last SAVED canvas pruned of
      the removed devices, never the draft, so the version history only ever snapshots
      saved states.

    Rides ``commit_fork_with_new_version`` exactly like save_fork; the reconcile hook
    re-reads the committed draft, latest version, and rows fresh, so a lost version
    race recomputes against the winner's committed state (including a winner save that
    replaced the draft). Idempotent: a replay finds nothing to release and returns
    ``changed`` False with no version appended (a draft-only scrub earns no version;
    drafts are cheap and fork_versions must only snapshot saved states).
    """
    removed = {str(d) for d in device_ids}
    fork_id = fork.id

    async def _latest_version() -> ForkVersion | None:
        return (
            (
                await db.execute(
                    select(ForkVersion)
                    .where(ForkVersion.fork_id == fork_id)
                    .order_by(ForkVersion.version_number.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def _current_rows() -> list[ForkConnection]:
        return (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork_id)))
            .scalars()
            .all()
        )

    async def _current_l3_rows() -> list[ForkL3Route]:
        # ADR 0014 phase 1 (issue #34): a removed device's L3 routing intent
        # releases outright, no edge-incidence reasoning needed (a route belongs
        # to the switch itself, not to any particular edge, unlike a wiring hop
        # that can be a surviving through-hop of a different edge).
        return (
            (
                await db.execute(
                    select(ForkL3Route).where(
                        ForkL3Route.fork_id == fork_id, ForkL3Route.device_id.in_(device_ids)
                    )
                )
            )
            .scalars()
            .all()
        )

    latest = await _latest_version()
    current_version = latest.version_number if latest is not None else 0
    saved_canvas = latest.canvas_data if latest is not None else None
    _, _, remaining_edge_ids, pruned_edge_ids = prune_canvas_for_devices(saved_canvas, removed)
    to_release = _rows_released_by_prune(
        await _current_rows(), removed, remaining_edge_ids, pruned_edge_ids
    )
    l3_to_release_precheck = await _current_l3_rows()
    pruned_draft, draft_changed, _, _ = prune_canvas_for_devices(fork.canvas_data, removed)

    if not to_release and not l3_to_release_precheck:
        if draft_changed:
            fork.canvas_data = pruned_draft
            await db.commit()
        return ForkPruneResult(
            fork_id=fork_id, version_number=current_version, changed=False, released=[]
        )

    result: dict = {}
    snapshot = ForkVersion(fork_id=fork_id, canvas_data=None)

    async def reconcile() -> None:
        # Re-read committed state fresh: identical to the setup reads on the first
        # pass, and recomputed against the winner's rows on a version-race retry
        # (which may have replaced the draft, appended a version, or already released
        # some rows). The direct column select bypasses the stale in-session value a
        # rollback reapply restores.
        committed_draft = (
            await db.execute(
                select(ReservationFork.canvas_data).where(ReservationFork.id == fork_id)
            )
        ).scalar_one()
        fork.canvas_data = prune_canvas_for_devices(committed_draft, removed)[0]

        latest_now = await _latest_version()
        pruned_saved, _, remaining_now, pruned_now = prune_canvas_for_devices(
            latest_now.canvas_data if latest_now is not None else None, removed
        )
        snapshot.canvas_data = pruned_saved

        release_rows = _rows_released_by_prune(
            await _current_rows(), removed, remaining_now, pruned_now
        )
        for row in release_rows:
            await db.delete(row)

        # ADR 0014 phase 1 (issue #34): release the removed devices' L3 routing
        # intent in the same transaction. Re-read fresh (mirroring the wiring
        # release just above) so a version-race retry recomputes against the
        # winner's committed rows rather than double-deleting or missing rows a
        # concurrent writer added.
        l3_release_rows = await _current_l3_rows()
        for row in l3_release_rows:
            await db.delete(row)

        await db.flush()
        result["released"] = [
            WireSpec(
                device_a_id=row.device_a_id,
                port_a=row.port_a,
                device_b_id=row.device_b_id,
                port_b=row.port_b,
                layer=row.layer,
                physical_connection_id=row.physical_connection_id,
                edge_key=row.edge_key,
            )
            for row in release_rows
        ]

    await reconcile()
    await commit_fork_with_new_version(db, fork, snapshot, reconcile=reconcile)

    return ForkPruneResult(
        fork_id=fork_id,
        version_number=snapshot.version_number,
        changed=True,
        released=result["released"],
    )


__all__ = [
    "CanvasWiringResolution",
    "ForkPruneResult",
    "ForkSaveResult",
    "WireSpec",
    "assert_endpoints_are_members",
    "assert_no_port_claims",
    "classify_element_edge",
    "connection_identity",
    "gate_l3_intent",
    "lock_port_claims",
    "node_to_device_map",
    "node_to_element_map",
    "prune_canvas_for_devices",
    "prune_fork_devices",
    "reconcile_by_identity",
    "reconcile_connection_sets",
    "reconcile_l3_route_sets",
    "resolve_canvas_wiring",
    "save_fork",
    "touched_devices_from_specs",
]
