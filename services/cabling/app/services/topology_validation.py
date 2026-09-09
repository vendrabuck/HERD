"""Topology canvas validation (R1 review fix on 2ade362c, ADR 0014 phase 1, issue #34).

Moved out of ``routes/topologies.py``'s ``_run_topology_validation`` into two
composable passes plus one orchestrator:

- ``validate_canvas_edges``: the original edge-BFS pass (missing_device/no_path/
  element classification against the physical Connection graph). Returns
  ``invalid_edges`` and ``device_ids``. Never makes an inventory call.
- ``validate_canvas_l3`` (``l3_validation.py``): the L3 routing-intent pass.
- ``run_full_topology_validation``: composes both for the two ``/validate`` routes.
  The loose canvas PUT and fork restore (``routes/forks.py``) call
  ``validate_canvas_edges`` alone, since their contract is "the draft stores
  regardless": they must never make an inventory call or 503 on an unsaved draft.

``touched_devices`` for the L3 pass (R2 review fix) is derived from
``resolve_canvas_wiring(canvas).specs`` (both endpoints of every resolved hop, port
constraints honored, transit devices included), NOT from this module's own edge-BFS
pass: a port-constrained edge whose port has no cable must make its switch
unattached even if some other resolvable path would have reached it, and a transit
device on a multi-hop resolved path must count as attached even though it is never
itself an edge endpoint. ``resolve_canvas_wiring`` is only called when
``canvas_has_l3`` says the canvas is worth the extra graph build and inventory
round trips; a topology with no ``data.l3`` anywhere pays for neither.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.topology import InvalidEdge, InvalidRoute, TopologyValidationResponse
from app.services.canvas_nodes import classify_element_edge, node_to_device_map, node_to_element_map
from app.services.fork_save_service import resolve_canvas_wiring, touched_devices_from_specs
from app.services.l3_intent import canvas_has_l3, walk_l3_nodes
from app.services.l3_validation import route_causes_invalid, validate_canvas_l3
from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths_batch_async


@dataclass(frozen=True)
class EdgeValidation:
    """The edge pass's outcome: every canvas edge's verdict, plus the canvas's
    device node ids (issue #701 phase 2's ``device_ids``, computed alongside the
    edge walk since both need the same ``node_to_device`` map).

    ``graph`` (S11 review fix, round 2) is the adjacency graph this pass built
    (empty when the canvas has no edges, in which case nothing downstream ever
    reads it), so ``run_full_topology_validation`` can hand it to
    ``resolve_canvas_wiring`` instead of paying for a second
    ``build_adjacency_graph`` load of the same canvas.
    """

    invalid_edges: list[InvalidEdge]
    device_ids: list[uuid.UUID]
    graph: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]] = field(default_factory=dict)


async def validate_canvas_edges(canvas: dict | None, db: AsyncSession) -> EdgeValidation:
    """Walk the topology canvas and check each edge against the cabling graph.

    Shared by both ``/validate`` routes (via ``run_full_topology_validation``) and,
    alone, by the fork loose canvas PUT and fork-version restore endpoints
    (``routes/forks.py``), whose contract is "the draft stores regardless": no
    inventory call, no 503, ever, from this function.
    """
    canvas = canvas or {}
    edges = canvas.get("edges") or []

    # node_id (React Flow id) to device_id map. Edges reference React Flow node ids,
    # not device ids; we resolve devices through this map.
    node_to_device = node_to_device_map(canvas)

    # node_id to network element id (ADR 0012 phase 1, issue #22). Shared with
    # resolve_canvas_wiring's classification via node_to_element_map so the validator
    # and the fork-save resolver agree on which edges are element attachments.
    node_to_element = node_to_element_map(canvas)

    # Issue #701: the canvas's device node ids, deduplicated and sorted, for
    # reservations' create-time membership check. Computed from node_to_device so a
    # dynamic placeholder or network element node (neither carries data.device.id)
    # never appears here regardless of edge presence.
    device_ids = sorted(set(node_to_device.values()))

    if not edges:
        return EdgeValidation(invalid_edges=[], device_ids=device_ids)

    # Scope the graph to the connected component(s) of the topology's devices.
    # Expansion still loads off-canvas intermediates (a patch panel that
    # physically realizes an edge), so no reachable path is dropped; it only
    # skips fabrics unrelated to this topology. Built once and reused across all
    # edges, as before.
    graph = await build_adjacency_graph(db, device_ids=set(node_to_device.values()))

    # First pass: skip proposal edges, classify missing-device edges immediately
    # (same reason and fields as before), and defer the rest for pathfinding.
    # `edge_results` is positional (one slot per edge in `edges`, None meaning
    # "not invalid" or "proposal, skipped"), so the final invalid list can be
    # reassembled in the exact original per-edge order regardless of how the
    # batched pathfind call below resolves its pairs. `pending` carries the
    # (edges-index, edge_id, layer, source_device, target_device) tuples for
    # every edge that needs a path lookup, in edge order.
    edge_results: list[InvalidEdge | None] = [None] * len(edges)
    pending: list[tuple[int, str, str | None, uuid.UUID, uuid.UUID]] = []

    for idx, edge in enumerate(edges):
        edge_id = str(edge.get("id") or "")
        edge_data = edge.get("data") or {}
        layer = edge_data.get("layer")
        # Skip proposal edges (not yet committed by the user).
        if edge_data.get("isProposal"):
            continue

        source_node = edge.get("source")
        target_node = edge.get("target")
        source_device = node_to_device.get(source_node) if source_node else None
        target_device = node_to_device.get(target_node) if target_node else None

        # Network element classification (ADR 0012 phase 1, issue #22), via the
        # classifier shared with resolve_canvas_wiring. Checked before the
        # missing_device fallback so an element edge is classified on its own terms
        # rather than as a dangling device reference.
        classification = classify_element_edge(edge, node_to_device, node_to_element)

        if classification == "element_to_element":
            edge_results[idx] = InvalidEdge(
                edge_id=edge_id,
                source_device_id=None,
                target_device_id=None,
                layer=layer,
                reason="element_to_element",
            )
            continue

        if classification == "element_edge_no_port":
            edge_results[idx] = InvalidEdge(
                edge_id=edge_id,
                source_device_id=source_device,
                target_device_id=target_device,
                layer=layer,
                reason="element_edge_no_port",
            )
            continue

        if classification == "attachment":
            # VALID declarative attachment: no BFS, not added to `pending`. An
            # element is not a physical thing the cabling graph could contain a
            # path to.
            continue

        if source_device is None or target_device is None:
            edge_results[idx] = InvalidEdge(
                edge_id=edge_id,
                source_device_id=source_device,
                target_device_id=target_device,
                layer=layer,
                reason="missing_device",
            )
            continue

        pending.append((idx, edge_id, layer, source_device, target_device))

    # Second pass: one batched BFS call over every resolvable pair instead of
    # one asyncio.to_thread hop per edge (issue #313, same fix class as
    # #249/#250). Results are returned in request order, matching `pending`
    # positionally, and an empty path-list means the same "no_path" outcome
    # find_all_shortest_paths_async would have produced for that pair.
    pairs = [(source_device, target_device) for _, _, _, source_device, target_device in pending]
    all_paths = await find_all_shortest_paths_batch_async(graph, pairs)

    for (idx, edge_id, layer, source_device, target_device), paths in zip(pending, all_paths):
        if not paths:
            edge_results[idx] = InvalidEdge(
                edge_id=edge_id,
                source_device_id=source_device,
                target_device_id=target_device,
                layer=layer,
                reason="no_path",
            )

    invalid = [result for result in edge_results if result is not None]
    return EdgeValidation(invalid_edges=invalid, device_ids=device_ids, graph=graph)


async def run_full_topology_validation(
    canvas: dict | None,
    db: AsyncSession,
    *,
    check_routes: bool = True,
) -> TopologyValidationResponse:
    """Compose the edge pass and (when applicable) the L3 pass into one response.

    ``check_routes=False`` (R11 review fix, issue #701's device-set PATCH path)
    skips the L3 pass entirely: no ``resolve_canvas_wiring`` call, no inventory
    call, ``invalid_routes`` empty. A device-set PATCH judges only physical
    connectivity for the reservation's revised membership; the topology's own
    routing intent was already judged valid at create time and is unrelated to
    which devices the booking currently includes.

    S10 review fix, round 2: the canvas's L3 nodes are parsed exactly once, via
    ``l3_intent.walk_l3_nodes``, and handed to ``validate_canvas_l3`` as already
    -parsed candidates/malformed entries rather than a raw canvas.

    S11 review fix, round 2: when the L3 pass needs ``resolve_canvas_wiring``,
    it reuses the adjacency graph ``validate_canvas_edges`` already built for
    this same canvas, rather than a second ``build_adjacency_graph`` load.
    """
    canvas = canvas or {}
    edge_validation = await validate_canvas_edges(canvas, db)

    invalid_routes: list[InvalidRoute] = []
    if check_routes and canvas_has_l3(canvas):
        wiring_resolution = await resolve_canvas_wiring(db, canvas, graph=edge_validation.graph)
        touched_devices = touched_devices_from_specs(wiring_resolution.specs)
        candidates, malformed = walk_l3_nodes(canvas)
        result = await validate_canvas_l3(candidates, malformed, touched_devices)
        invalid_routes = result.invalid_routes

    # S12 review fix, round 2: an informational l3_duplicate_route entry never
    # makes the topology invalid.
    valid = not edge_validation.invalid_edges and not any(
        route_causes_invalid(r) for r in invalid_routes
    )
    return TopologyValidationResponse(
        valid=valid,
        invalid_edges=edge_validation.invalid_edges,
        device_ids=edge_validation.device_ids,
        invalid_routes=invalid_routes,
    )


__all__ = [
    "EdgeValidation",
    "run_full_topology_validation",
    "validate_canvas_edges",
]
