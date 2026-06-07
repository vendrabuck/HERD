"""Concurrency-safe allocation of TopologyVersion.version_number.

Both the canvas-changing PUT /topologies/{id} and POST .../restore append a new
TopologyVersion whose version_number is max(version_number)+1 for the topology.
That read-then-insert is not atomic: two concurrent writers on the same topology
both read max=N and both try to insert N+1. The unique constraint
uq_topology_versions_topology_version on (topology_id, version_number) lets one
commit and fails the other with IntegrityError, which would otherwise surface as
a raw 500.

The database is the arbiter here, exactly like find_or_assign_vlan in the
execution service: we attempt the insert, and on a unique-constraint violation we
roll back, recompute max+1 against the now-updated rows, and retry under a small
bounded cap. A retry loop (rather than SELECT ... FOR UPDATE on the parent row)
matches the existing convention and avoids holding a row lock across the request.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.topology import Topology, TopologyVersion

logger = logging.getLogger(__name__)

# Each retry recomputes max+1 against the committed rows, so a small cap absorbs
# realistic contention on a single topology; exhaustion signals a real anomaly.
_MAX_ALLOCATE_RETRIES = 5


async def commit_with_new_version(
    db: AsyncSession,
    topology: Topology,
    snapshot: TopologyVersion,
) -> None:
    """Commit a topology mutation together with a freshly numbered version snapshot.

    The caller has already applied its field changes to `topology` and built
    `snapshot` with every field set except version_number. This function assigns
    version_number = max+1, adds the snapshot, and commits. On a unique-constraint
    collision with a concurrent writer it rolls back, re-applies the pending
    topology changes (rollback expires them), recomputes max+1, and retries up to
    a bounded cap.

    Raises IntegrityError if contention persists past the retry cap, so a genuine
    anomaly still surfaces rather than silently corrupting the version sequence.
    """
    # Snapshot the caller's intended topology field values so we can re-apply them
    # after a rollback expires the pending state.
    pending_changes = {
        "name": topology.name,
        "canvas_data": topology.canvas_data,
        "modified_by": topology.modified_by,
    }

    for attempt in range(_MAX_ALLOCATE_RETRIES):
        max_number = (
            await db.execute(
                select(func.max(TopologyVersion.version_number)).where(
                    TopologyVersion.topology_id == topology.id
                )
            )
        ).scalar() or 0
        snapshot.version_number = max_number + 1
        db.add(snapshot)
        try:
            await db.commit()
            return
        except IntegrityError:
            await db.rollback()
            if attempt == _MAX_ALLOCATE_RETRIES - 1:
                logger.warning(
                    "Version allocation for topology %s exhausted %d retries",
                    topology.id,
                    _MAX_ALLOCATE_RETRIES,
                )
                raise
            # Rollback expired both the snapshot's number and the topology field
            # changes; re-apply the topology changes and loop to recompute max+1.
            topology.name = pending_changes["name"]
            topology.canvas_data = pending_changes["canvas_data"]
            topology.modified_by = pending_changes["modified_by"]
