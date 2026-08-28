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

import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.models.fork import ForkVersion, ReservationFork
from app.models.topology import Topology, TopologyVersion

logger = logging.getLogger(__name__)

# Each retry recomputes max+1 against the committed rows, so a small cap absorbs
# realistic contention on a single scope (one topology, or one fork); exhaustion
# signals a real anomaly.
_MAX_ALLOCATE_RETRIES = 5


async def _commit_with_new_version(
    db: AsyncSession,
    *,
    version_column: InstrumentedAttribute,
    scope_column: InstrumentedAttribute,
    scope_id: uuid.UUID,
    snapshot: Any,
    reapply_pending: Callable[[], Any],
    scope_label: str,
) -> None:
    """Allocate ``max+1`` under a unique constraint and commit, with bounded retry.

    The shared machinery behind both topology-version and fork-version allocation
    (issue #25 P3a, ADR 0006 Decision 3). ``version_column`` is the version_number
    column of the snapshot's model, ``scope_column``/``scope_id`` scope the ``max``
    query to one owner (topology_id or fork_id), and ``reapply_pending`` re-applies
    the owner-row field changes a rollback expires. It may be sync (the topology path)
    or async (the fork save path, which must also re-stage its release-before-build
    delta and re-run the port-claim query against the winner's committed rows);
    an awaitable return is awaited. Concurrent writers collide on the unique
    constraint; the loser rolls back, recomputes ``max+1`` against the now-committed
    rows, and retries up to ``_MAX_ALLOCATE_RETRIES``. Past the cap the IntegrityError
    propagates rather than corrupting the version sequence.
    """
    for attempt in range(_MAX_ALLOCATE_RETRIES):
        max_number = (
            await db.execute(select(func.max(version_column)).where(scope_column == scope_id))
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
                    "Version allocation for %s %s exhausted %d retries",
                    scope_label,
                    scope_id,
                    _MAX_ALLOCATE_RETRIES,
                )
                raise
            # Rollback expired both the snapshot's number and the owner-row field
            # changes; re-apply the owner changes and loop to recompute max+1. The
            # fork path's reapply is a coroutine, so await an awaitable result.
            result = reapply_pending()
            if inspect.isawaitable(result):
                await result


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

    def _reapply() -> None:
        topology.name = pending_changes["name"]
        topology.canvas_data = pending_changes["canvas_data"]
        topology.modified_by = pending_changes["modified_by"]

    await _commit_with_new_version(
        db,
        version_column=TopologyVersion.version_number,
        scope_column=TopologyVersion.topology_id,
        scope_id=topology.id,
        snapshot=snapshot,
        reapply_pending=_reapply,
        scope_label="topology",
    )


async def commit_fork_with_new_version(
    db: AsyncSession,
    fork: ReservationFork,
    snapshot: ForkVersion,
    reconcile: Callable[[], Any] | None = None,
) -> None:
    """Commit a fork mutation together with a freshly numbered fork_versions snapshot.

    The fork analogue of ``commit_with_new_version`` (issue #25 P3a). The caller has
    already applied its canvas change to `fork` and built `snapshot` with every field
    set except version_number. Allocation scopes to ``fork_id`` under
    ``uq_fork_versions_fork_version`` and shares the same rollback-recompute-retry
    loop, so concurrent saves of one fork serialize on the database exactly as
    concurrent topology writers do.

    ``reconcile`` is the optional save-reconcile hook (ADR 0006 open risk 2). On a
    version-race retry the rollback expires not just the canvas change but the whole
    release-before-build delta, so the loser must re-read the old set, re-run the
    cross-reservation port-claim query against the winner's committed rows, and
    re-stage its deletes and inserts before the next version allocation. Passing it
    here threads that recompute into the same retry loop; it is awaited after the
    canvas reapply on each retry.

    Also captures and reapplies ``fork.draft_restored_from_id`` alongside
    ``canvas_data`` (issue #622): a save that consumes the restore-to-draft marker
    (by copying it onto ``snapshot.restored_from_id`` and clearing the fork row's
    copy before calling this) must not let a version-race rollback silently restore
    the pre-clear value. A caller that never touches the field (prune-devices) just
    reapplies its current, unchanged value, a harmless no-op.
    """
    pending_changes = {
        "canvas_data": fork.canvas_data,
        "draft_restored_from_id": fork.draft_restored_from_id,
    }

    async def _reapply() -> None:
        fork.canvas_data = pending_changes["canvas_data"]
        fork.draft_restored_from_id = pending_changes["draft_restored_from_id"]
        if reconcile is not None:
            await reconcile()

    await _commit_with_new_version(
        db,
        version_column=ForkVersion.version_number,
        scope_column=ForkVersion.fork_id,
        scope_id=fork.id,
        snapshot=snapshot,
        reapply_pending=_reapply,
        scope_label="fork",
    )
