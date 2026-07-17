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


__all__ = [
    "ForkSaveResult",
    "WireSpec",
    "connection_identity",
    "node_to_device_map",
    "reconcile_connection_sets",
    "resolve_canvas_wiring",
    "save_fork",
]
