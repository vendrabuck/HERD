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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fork import ForkConnection, ForkStatus_ACTIVE, ForkVersion, ReservationFork
from app.models.topology import Topology, TopologyVersion
from app.services.fork_save_service import resolve_canvas_wiring

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


async def _snapshot_connections(
    db: AsyncSession,
    fork_id: uuid.UUID,
    canvas: dict | None,
    created_by: str,
) -> None:
    """Seed fork_connections from parent canvas edges resolved to physical paths.

    Open risk #1's recommended rule. Delegates to the shared ``resolve_canvas_wiring``
    resolver (the same one the save-reconcile uses, issue #25 P3a) to turn committed
    canvas edges into per-hop L1 WireSpecs, then materializes each as a fork_connection
    row stamped with ``created_by``. Sharing the resolver keeps fork-on-activation and
    save-time wiring byte-for-byte identical, including multi-hop paths and shared-hop
    de-duplication.
    """
    for spec in await resolve_canvas_wiring(db, canvas):
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

    # The guarded region must cover the INSERT, not just the commit (issue #304).
    # Postgres checks the reservation_id unique constraint at flush time, so on the
    # concurrent-activation race the loser's IntegrityError surfaces here, at
    # db.flush(), on the most likely interleavings: it raises immediately if the
    # winner already committed, or blocks on the winner's uncommitted index entry and
    # raises when the winner commits. A try wrapping only db.commit() lets that flush
    # error escape unhandled. _snapshot_connections's autoflushing SELECTs and the
    # fork_versions insert share the same transaction and constraints, so they belong
    # inside the guard too.
    try:
        await db.flush()
        await _snapshot_connections(db, fork.id, forked_canvas, created_by)
        db.add(
            ForkVersion(
                fork_id=fork.id,
                version_number=1,
                canvas_data=forked_canvas,
            )
        )
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
