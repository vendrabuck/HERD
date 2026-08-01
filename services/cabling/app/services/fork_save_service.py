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

Cross-reservation port-claim enforcement (Decision 4) runs after computing
``to_build``: a physical (device, port) endpoint wired by another ACTIVE fork may
not be claimed here, and a hit fails the save with 409. Both the reconcile staging
and the port-claim query re-run inside the version-allocation retry loop (ADR open
risk 2), so a save that lost the version race recomputes against the winner's
now-committed rows rather than half-applying.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkVersion, ReservationFork
from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths_async
from app.services.version_service import commit_fork_with_new_version


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


async def resolve_canvas_wiring(db: AsyncSession, canvas: dict | None) -> list[WireSpec]:
    """Resolve a canvas's committed edges to intended physical wiring (WireSpecs).

    The shared resolver behind both fork-on-activation snapshotting and save-reconcile
    (issue #25 P3a). For each committed (non-proposal) canvas edge between two
    resolvable devices it picks the first shortest physical path and records every hop
    as an L1 WireSpec carrying its backing physical connection id. Multi-hop paths
    (an off-canvas patch panel between the endpoints) yield one WireSpec per cable, and
    two edges sharing a hop de-duplicate on the path-orientation key so the same cable
    is not emitted twice. Save-time normalization (see connection_identity) collapses
    any remaining opposite-orientation duplicates.
    """
    if not canvas:
        return []
    edges = canvas.get("edges") or []
    if not edges:
        return []

    node_to_device = node_to_device_map(canvas)
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

    for edge in edges:
        edge_data = edge.get("data") or {}
        if edge_data.get("isProposal"):
            continue
        source_device = node_to_device.get(edge.get("source"))
        target_device = node_to_device.get(edge.get("target"))
        if source_device is None or target_device is None:
            continue

        # The canvas edge id, carried onto every hop this edge resolves to so the
        # consumer can group them (issue #345 P3b). Coerced to str for the String(255)
        # column; a missing id leaves the hops ungrouped (NULL). A hop shared by two
        # edges de-duplicates on the path-orientation ``seen`` key below, so it keeps
        # the edge id of whichever edge claimed it first.
        raw_edge_id = edge.get("id")
        edge_key = str(raw_edge_id) if raw_edge_id is not None else None

        paths = await find_all_shortest_paths_async(graph, source_device, target_device)
        if not paths:
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
    return specs


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
    new_specs = await resolve_canvas_wiring(db, canvas_data)
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
    snapshot = ForkVersion(fork_id=fork_id, canvas_data=canvas_data)
    await commit_fork_with_new_version(db, fork, snapshot, reconcile=reconcile)

    return ForkSaveResult(
        fork_id=fork_id,
        version_number=snapshot.version_number,
        released=result["released"],
        built=result["built"],
        unchanged_count=result["unchanged_count"],
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
    "ForkPruneResult",
    "ForkSaveResult",
    "WireSpec",
    "connection_identity",
    "node_to_device_map",
    "prune_canvas_for_devices",
    "prune_fork_devices",
    "reconcile_connection_sets",
    "resolve_canvas_wiring",
    "save_fork",
]
