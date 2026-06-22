"""Fork-on-activation: deep-copy a parent topology into an editable per-reservation
fork plus snapshot its relevant physical wiring (issue #25, Phase 2).

See docs/design/0001-editable-reservation-topologies.md (Decision 2 contract #1,
Decision 3 pin, and open risk #1 on snapshot scope).

The connection-snapshot scope follows the design's recommended rule (open risk #1):
seed fork_connections from the parent canvas EDGES resolved to physical paths via
find_all_shortest_paths, NOT the whole global connection graph. For each committed
(non-proposal) canvas edge between two resolvable devices, we pick the first
shortest path and record each physical hop as an L1 fork_connection carrying its
backing physical connection id. Edges with no path are skipped (the booking is
already gated on connectivity by validate/internal at create time); they leave no
wire, exactly as an unreachable physical route would.
"""

import copy
import logging
import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkVersion, ReservationFork
from app.models.topology import Topology, TopologyVersion
from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths_async

logger = logging.getLogger(__name__)


async def _resolve_parent_canvas(
    db: AsyncSession,
    parent_topology_id: uuid.UUID | None,
    parent_version_id: uuid.UUID | None,
) -> tuple[dict | None, uuid.UUID | None]:
    """Return (canvas_to_fork_from, pinned_version_id).

    Decision 3 Case B pins the parent TopologyVersion at activation, not at create.
    Two ways in:

    - The caller supplied an explicit parent_version_id: fork from that immutable
      snapshot and pin it.
    - The caller supplied only parent_topology_id (the design's "cabling resolves
      'current' itself" branch): resolve the parent's current max TopologyVersion,
      fork from that snapshot, and pin it. This closes the create-to-activation
      window without reservations needing a second round-trip. If the parent has
      no versions yet, fall back to its live canvas with no pin.

    With no parent topology at all (Case A lazy-create, handled by the caller's
    skip) there is nothing to copy.
    """
    if parent_version_id is not None:
        version = await db.get(TopologyVersion, parent_version_id)
        if version is not None:
            return version.canvas_data, version.id

    if parent_topology_id is not None:
        current = (
            await db.execute(
                select(TopologyVersion)
                .where(TopologyVersion.topology_id == parent_topology_id)
                .order_by(TopologyVersion.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if current is not None:
            return current.canvas_data, current.id
        topology = await db.get(Topology, parent_topology_id)
        if topology is not None:
            return topology.canvas_data, None

    return None, None


def _node_to_device_map(canvas: dict) -> dict[str, uuid.UUID]:
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


async def _snapshot_connections(
    db: AsyncSession,
    fork_id: uuid.UUID,
    canvas: dict | None,
    created_by: str,
) -> None:
    """Seed fork_connections from parent canvas edges resolved to physical paths.

    Open risk #1's recommended rule. For each committed canvas edge, resolve a
    shortest physical path between its endpoint devices and record every hop as an
    L1 fork_connection backed by the physical connections row that realizes it.
    De-duplicates on the fork_connections unique key so two canvas edges sharing a
    hop do not collide on insert.
    """
    if not canvas:
        return
    edges = canvas.get("edges") or []
    if not edges:
        return

    node_to_device = _node_to_device_map(canvas)
    # Scope to the connected component(s) of the canvas devices (the fork's own
    # fabric), matching this module's stated rule: snapshot from the parent
    # canvas edges resolved to physical paths, not the whole global graph.
    # Component expansion still loads off-canvas intermediates, so every hop on
    # a chosen path is present.
    graph = await build_adjacency_graph(db, device_ids=set(node_to_device.values()))

    # Look up the backing physical connection id for an (a, port_a, b, port_b) hop.
    # The cabling graph is undirected, so match either orientation. Scope the
    # phys lookup to the same component devices the graph loaded (graph.keys()),
    # so every connection that can appear on a returned path is indexed while
    # unrelated fabrics are skipped.
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
    for conn_id, da, pa, db_, pb in phys_rows:
        phys_index[(da, pa, db_, pb)] = conn_id
        phys_index[(db_, pb, da, pa)] = conn_id

    seen: set[tuple[uuid.UUID, str, uuid.UUID, str, str]] = set()

    for edge in edges:
        edge_data = edge.get("data") or {}
        if edge_data.get("isProposal"):
            continue
        source_device = node_to_device.get(edge.get("source"))
        target_device = node_to_device.get(edge.get("target"))
        if source_device is None or target_device is None:
            continue

        paths = await find_all_shortest_paths_async(graph, source_device, target_device)
        if not paths:
            # Unreachable edge: nothing to wire. Create-time validation already
            # gates the booking on connectivity, so this is the rare deleted-cable
            # window, not a normal case.
            continue

        path = paths[0]
        # Each adjacent pair of hops is one physical cable: the first hop's
        # port_out connects to the next hop's port_in.
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
            db.add(
                ForkConnection(
                    fork_id=fork_id,
                    device_a_id=da,
                    port_a=pa,
                    device_b_id=db_dev,
                    port_b=pb,
                    layer="L1",
                    physical_connection_id=phys_index.get((da, pa, db_dev, pb)),
                    created_by=created_by,
                )
            )


async def create_fork(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    parent_topology_id: uuid.UUID | None,
    parent_version_id: uuid.UUID | None,
    created_by: str = "system",
) -> ReservationFork:
    """Create (or return the existing) fork for a reservation.

    Idempotent on reservation_id: a retried activation re-POST returns the already
    created fork rather than building a second one. Deep-copies the pinned parent
    canvas, snapshots its relevant physical wiring into fork_connections, and
    writes fork_versions v1, all in one transaction.
    """
    existing = (
        await db.execute(
            select(ReservationFork).where(ReservationFork.reservation_id == reservation_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    parent_canvas, pinned_version_id = await _resolve_parent_canvas(
        db, parent_topology_id, parent_version_id
    )
    forked_canvas = None if parent_canvas is None else copy.deepcopy(parent_canvas)

    fork = ReservationFork(
        reservation_id=reservation_id,
        parent_topology_id=parent_topology_id,
        parent_version_id=pinned_version_id,
        canvas_data=forked_canvas,
        status=ForkStatus_ACTIVE,
    )
    db.add(fork)
    await db.flush()

    await _snapshot_connections(db, fork.id, forked_canvas, created_by)

    db.add(
        ForkVersion(
            fork_id=fork.id,
            version_number=1,
            canvas_data=forked_canvas,
        )
    )

    try:
        await db.commit()
    except IntegrityError:
        # A concurrent activation committed its fork first (unique reservation_id).
        # Roll back and return the winner so the contract stays idempotent. This is
        # critical for reservations.create retries: the activation->fork transition
        # is not atomic at the application level, so two concurrent PENDINGs both
        # call create_fork. The DB unique constraint on (reservation_id) serializes
        # them; the loser rolls back and fetches the winner's fork.
        await db.rollback()
        existing = (
            await db.execute(
                select(ReservationFork).where(ReservationFork.reservation_id == reservation_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing

    await db.refresh(fork)
    return fork
