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
"""

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkVersion, ReservationFork
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


@dataclass
class ForkPruneResult:
    """The device-prune outcome (ADR 0009 Decision 6 REMOVE half, issue #459).

    ``changed`` is True iff wiring was released and a fork_versions row appended;
    a no-op replay (nothing left to release) returns the current latest version
    with ``changed`` False so the caller stages nothing.
    """

    fork_id: uuid.UUID
    version_number: int
    changed: bool
    released: list[WireSpec]


def node_to_device_map(canvas: dict) -> dict[str, uuid.UUID]:
    """Map React Flow node ids to device UUIDs, mirroring _run_topology_validation."""
    nodes = canvas.get("nodes") or []
    mapping: dict[str, uuid.UUID] = {}
    for node in nodes:
        node_id = node.get("id")
        device_id_str = ((node.get("data") or {}).get("device") or {}).get("id")
        if not node_id or not device_id_str:
            continue
        try:
            mapping[node_id] = uuid.UUID(device_id_str)
        except (ValueError, TypeError):
            continue
    return mapping


def node_to_element_map(canvas: dict) -> dict[str, str]:
    """Map React Flow node ids to network element ids (ADR 0012 phase 1, issue #22).

    Populated from nodes whose ``type`` is ``"networkElementNode"``, keyed by node id,
    valued by ``data.element.id`` (the client-minted element UUID). A node of that type
    with no ``data.element.id`` falls back to the node id itself, so a malformed element
    node still classifies as an element rather than silently vanishing from the map.
    Shared by ``_run_topology_validation`` and ``resolve_canvas_wiring`` so both
    classify an element edge identically.
    """
    nodes = canvas.get("nodes") or []
    mapping: dict[str, str] = {}
    for node in nodes:
        if node.get("type") != "networkElementNode":
            continue
        node_id = node.get("id")
        if not node_id:
            continue
        element_id = ((node.get("data") or {}).get("element") or {}).get("id")
        mapping[node_id] = element_id or node_id
    return mapping


def classify_element_edge(
    edge: dict,
    node_to_device: dict[str, uuid.UUID],
    node_to_element: dict[str, str],
) -> str | None:
    """Classify one canvas edge against the element/device maps (ADR 0012 phase 1).

    The single shared classifier behind both ``_run_topology_validation`` and
    ``resolve_canvas_wiring``, so the validator and the fork-save resolver agree on
    which edges are element attachments and which of those are valid. Direction is
    accepted either way: the frontend normalizes device-as-source, but an older client
    or a hand-edited import may hand back the element first.

    Returns one of:

    - ``"attachment"``: exactly one endpoint is a network element, the other is a
      known device, and the device-side port name (``target_port_name`` when the
      device is the target, ``source_port_name`` when the device is the source) is
      non-empty. This is the only shape ``_run_topology_validation`` accepts and the
      only shape ``resolve_canvas_wiring`` should count.
    - ``"element_to_element"``: both endpoints are network elements.
    - ``"element_edge_no_port"``: exactly one endpoint is a network element, the other
      is a known device, but the device-side port name is missing or empty.
    - ``None``: not an element edge (neither endpoint is in ``node_to_element``), OR
      exactly one endpoint is an element and the other resolves to no known device
      either. That second case is deliberately left for the caller's own
      missing-device/unresolvable-endpoint handling, since a dangling node reference
      is a dangling node reference regardless of what the other end is.
    """
    source_node = edge.get("source")
    target_node = edge.get("target")
    source_is_element = source_node in node_to_element
    target_is_element = target_node in node_to_element

    if not source_is_element and not target_is_element:
        return None

    if source_is_element and target_is_element:
        return "element_to_element"

    source_device = node_to_device.get(source_node) if source_node else None
    target_device = node_to_device.get(target_node) if target_node else None
    device_side_id = target_device if source_is_element else source_device
    if device_side_id is None:
        return None

    edge_data = edge.get("data") or {}
    device_side_port = (
        edge_data.get("target_port_name")
        if source_is_element
        else edge_data.get("source_port_name")
    )
    if not device_side_port:
        return "element_edge_no_port"

    return "attachment"


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
    check below, so only the edges ``_run_topology_validation`` would accept as a valid
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
    old_map: dict[tuple, ForkConnection] = {_row_identity(r): r for r in old_rows}
    new_map: dict[tuple, WireSpec] = {_spec_identity(s): s for s in new_specs}
    old_keys = set(old_map)
    new_keys = set(new_map)
    to_release = [old_map[k] for k in old_keys - new_keys]
    to_build = [new_map[k] for k in new_keys - old_keys]
    unchanged_count = len(old_keys & new_keys)
    return to_release, to_build, unchanged_count


async def _assert_no_port_claims(
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
    loop so a save that lost the race sees the winner's committed claims.
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
    created_by: str = "system",
) -> ForkSaveResult:
    """Reconcile a fork's wiring against a submitted canvas and append a version.

    Resolves the canvas once to the intended set, then reconciles and commits under
    the version-allocation retry loop. The reconcile (fresh old-set read, port-claim
    check, release-before-build staging) runs on the first pass here and re-runs inside
    ``commit_fork_with_new_version``'s reapply hook on every retry, so a lost version
    race recomputes against committed rows and a mid-reconcile failure rolls back the
    whole save (no half-apply, no orphan version). The caller has already refused an
    ARCHIVED fork.
    """
    # Capture the id up front: a version-race rollback expires ``fork``, and a later
    # lazy ``fork.id`` read inside the reconcile closure would attempt synchronous IO
    # off the async loop. Setting ``fork.canvas_data`` is a plain attribute write and
    # needs no load.
    fork_id = fork.id
    wiring_resolution = await resolve_canvas_wiring(db, canvas_data)
    new_specs = wiring_resolution.specs
    result: dict = {}

    async def reconcile() -> None:
        fork.canvas_data = canvas_data
        old_rows = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork_id)))
            .scalars()
            .all()
        )
        to_release, to_build, unchanged_count = reconcile_connection_sets(old_rows, new_specs)
        await _assert_no_port_claims(db, fork_id, to_build)

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

    latest = await _latest_version()
    current_version = latest.version_number if latest is not None else 0
    saved_canvas = latest.canvas_data if latest is not None else None
    _, _, remaining_edge_ids, pruned_edge_ids = prune_canvas_for_devices(saved_canvas, removed)
    to_release = _rows_released_by_prune(
        await _current_rows(), removed, remaining_edge_ids, pruned_edge_ids
    )
    pruned_draft, draft_changed, _, _ = prune_canvas_for_devices(fork.canvas_data, removed)

    if not to_release:
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
    "connection_identity",
    "node_to_device_map",
    "node_to_element_map",
    "prune_canvas_for_devices",
    "prune_fork_devices",
    "reconcile_connection_sets",
    "resolve_canvas_wiring",
    "save_fork",
]
