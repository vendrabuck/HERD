"""Internal fork lifecycle endpoints (issue #25, Phase 2).

Reservations is the lifecycle authority and calls cabling at activation to create
the editable per-reservation fork. All endpoints here are service-to-service and
guarded by X-Internal-Token exactly like validate_topology_internal: the booking
user does not necessarily own the parent topology, so a JWT-forward would 403.

See docs/design/0001-editable-reservation-topologies.md (Decision 2).
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.fork import (
    ForkConnection,
    ForkStatus_ACTIVE,
    ForkStatus_ARCHIVED,
    ForkVersion,
    ReservationFork,
)
from app.models.topology import Topology
from app.routes.topologies import _run_topology_validation
from app.schemas.fork import (
    ActiveForkEntry,
    ActiveForkListResponse,
    ForkArchiveResponse,
    ForkCanvasUpdate,
    ForkCanvasUpdateResponse,
    ForkConnectionDelta,
    ForkConnectionResponse,
    ForkCreate,
    ForkCreateResponse,
    ForkDetailResponse,
    ForkPruneRequest,
    ForkPruneResponse,
    ForkRestoreResponse,
    ForkSaveRequest,
    ForkSaveResponse,
    ForkVersionDetailResponse,
    ForkVersionSummary,
)
from app.services.fork_save_service import WireSpec, prune_fork_devices, save_fork
from app.services.fork_service import create_fork

router = APIRouter(prefix="/internal/forks", tags=["forks"])


def _check_internal_token(token: str) -> None:
    if not internal_token_matches(token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")


async def _load_fork(db: AsyncSession, reservation_id: uuid.UUID) -> ReservationFork:
    fork = (
        await db.execute(
            select(ReservationFork).where(ReservationFork.reservation_id == reservation_id)
        )
    ).scalar_one_or_none()
    if fork is None:
        raise HTTPException(status_code=404, detail="Fork not found")
    return fork


async def _load_fork_version(
    db: AsyncSession, fork_id: uuid.UUID, version_id: uuid.UUID
) -> ForkVersion:
    version = await db.get(ForkVersion, version_id)
    if version is None or version.fork_id != fork_id:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


def _to_delta(spec: WireSpec) -> ForkConnectionDelta:
    """Map a resolved WireSpec to its wire-facing ForkConnectionDelta shape."""
    return ForkConnectionDelta(
        device_a_id=spec.device_a_id,
        port_a=spec.port_a,
        device_b_id=spec.device_b_id,
        port_b=spec.port_b,
        layer=spec.layer,
        physical_connection_id=spec.physical_connection_id,
        edge_key=spec.edge_key,
    )


@router.post("", response_model=ForkCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_fork_internal(
    body: ForkCreate,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Create-or-return the fork for a reservation at activation.

    Idempotent on reservation_id (a retried activation returns the existing fork).
    Deep-copies the pinned parent canvas, snapshots the parent's relevant physical
    connections into fork_connections, and writes fork_versions v1.
    """
    _check_internal_token(x_internal_token)

    fork = await create_fork(
        db,
        reservation_id=body.reservation_id,
        parent_topology_id=body.parent_topology_id,
        parent_version_id=body.parent_version_id,
        created_by=body.created_by or "system",
    )

    version_number = (
        await db.execute(
            select(func.max(ForkVersion.version_number)).where(ForkVersion.fork_id == fork.id)
        )
    ).scalar() or 1

    return ForkCreateResponse(fork_id=fork.id, version_number=version_number)


@router.get("", response_model=ActiveForkListResponse)
async def list_active_forks_internal(
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """List reservation_ids of every fork still in status ACTIVE (issue #25 P3a).

    Feeds reservations' standing archive reconciler (ADR 0006 Decision 5): cabling
    keeps a fork ACTIVE until reservations archives it on teardown, so an ACTIVE
    fork whose reservation has ended (a crash between the terminal transition and
    the best-effort archive, or any pre-phase-3 fork that predates the teardown
    wiring) is a zombie that false-contends for port claims. This is the read side;
    reservations decides which of these to freeze. ARCHIVED forks are never listed,
    so an already-frozen fork drops out naturally. Ordered by created_at for stable
    pagination.
    """
    _check_internal_token(x_internal_token)

    total = (
        await db.execute(
            select(func.count())
            .select_from(ReservationFork)
            .where(ReservationFork.status == ForkStatus_ACTIVE)
        )
    ).scalar() or 0

    rows = (
        await db.execute(
            select(ReservationFork.id, ReservationFork.reservation_id)
            .where(ReservationFork.status == ForkStatus_ACTIVE)
            .order_by(ReservationFork.created_at)
            .offset(skip)
            .limit(limit)
        )
    ).all()

    # Latest fork_version per fork on this page, in one grouped query (ADR 0007
    # Decision 2). A fork always has at least a v1 from create_fork, so a missing
    # row would be anomalous; default to 0 so the heal treats it as "behind" rather
    # than crashing.
    fork_ids = [fork_id for fork_id, _ in rows]
    latest_by_fork: dict[uuid.UUID, int] = {}
    if fork_ids:
        version_rows = (
            await db.execute(
                select(
                    ForkVersion.fork_id,
                    func.max(ForkVersion.version_number),
                )
                .where(ForkVersion.fork_id.in_(fork_ids))
                .group_by(ForkVersion.fork_id)
            )
        ).all()
        latest_by_fork = {fork_id: version for fork_id, version in version_rows}

    return ActiveForkListResponse(
        reservation_ids=[reservation_id for _, reservation_id in rows],
        forks=[
            ActiveForkEntry(
                reservation_id=reservation_id,
                latest_fork_version=latest_by_fork.get(fork_id, 0),
            )
            for fork_id, reservation_id in rows
        ],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{reservation_id}", response_model=ForkDetailResponse)
async def get_fork_internal(
    reservation_id: uuid.UUID,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Return a reservation's fork: metadata, current canvas, wiring, and versions.

    404 when no fork exists yet (reservations lazy-creates on first edit through the
    idempotent POST above). Read-only; issue #25 P3a, ADR 0006 Decision 2.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)

    connections = (
        (
            await db.execute(
                select(ForkConnection)
                .where(ForkConnection.fork_id == fork.id)
                .order_by(ForkConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    versions = (
        (
            await db.execute(
                select(ForkVersion)
                .where(ForkVersion.fork_id == fork.id)
                .order_by(ForkVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )

    return ForkDetailResponse(
        id=fork.id,
        reservation_id=fork.reservation_id,
        parent_topology_id=fork.parent_topology_id,
        parent_version_id=fork.parent_version_id,
        status=fork.status,
        canvas_data=fork.canvas_data,
        draft_restored_from_id=fork.draft_restored_from_id,
        created_at=fork.created_at,
        updated_at=fork.updated_at,
        connections=[ForkConnectionResponse.model_validate(c) for c in connections],
        versions=[ForkVersionSummary.model_validate(v) for v in versions],
    )


@router.get(
    "/{reservation_id}/versions/{version_id}",
    response_model=ForkVersionDetailResponse,
)
async def get_fork_version_internal(
    reservation_id: uuid.UUID,
    version_id: uuid.UUID,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Return one fork_versions row's full canvas payload (issue #622).

    The read side of the history panel's version preview and diff: unlike the GET
    above, this carries the version's own canvas_data rather than the fork's current
    draft. 404 when the fork does not exist, or when the version exists but belongs
    to a different fork (never leaks a foreign version's existence). Read-only; no
    status check, mirroring GET /{reservation_id} which is allowed for any
    reservation status.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)
    version = await _load_fork_version(db, fork.id, version_id)

    return ForkVersionDetailResponse(
        id=version.id,
        fork_id=version.fork_id,
        version_number=version.version_number,
        restored_from_id=version.restored_from_id,
        created_at=version.created_at,
        canvas_data=version.canvas_data,
    )


@router.post(
    "/{reservation_id}/versions/{version_id}/restore",
    response_model=ForkRestoreResponse,
)
async def restore_fork_version_internal(
    reservation_id: uuid.UUID,
    version_id: uuid.UUID,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Restore to draft: copy a version's canvas onto the fork's draft (issue #622).

    Restore-to-draft, never restore-and-reconcile (ADR 0006 addendum, 2026-08-28,
    revised after PR #623 review): this replaces ONLY the fork's draft canvas_data
    and sets draft_restored_from_id = version_id on the fork row. It deliberately
    appends NO fork_versions row of its own: a version means something was
    reconciled (see the loose canvas PUT just below, which also appends none), and
    the standing wiring-heal reconciler (ADR 0007 Decision 2) relies on cabling's
    latest fork_version only outrunning reservations' wiring ledger when a save's
    staging was actually missed. An appended version here would falsely trip that
    heal for a canvas-only change. The marker rides on the fork row until the next
    save consumes it (see save_fork_internal), so it is NOT cleared here.

    Never touches fork_connections, the wiring ledger, or the outbox; nothing is
    wired until the caller runs the existing save-reconcile endpoint. 404 when the
    fork or the version (scoped to that fork) does not exist; 409 when the fork is
    ARCHIVED, the same wording as the loose canvas PUT and the save endpoint.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")
    version = await _load_fork_version(db, fork.id, version_id)

    restored_canvas = version.canvas_data
    fork.canvas_data = restored_canvas
    fork.draft_restored_from_id = version.id
    # Same validation the loose canvas PUT runs, on the same terms: reported, not
    # gated on. This also mirrors that endpoint in appending no version and not
    # touching fork_connections.
    validation = await _run_topology_validation(Topology(canvas_data=restored_canvas), db)
    fork_id = fork.id
    draft_restored_from_id = fork.draft_restored_from_id
    await db.commit()

    return ForkRestoreResponse(
        id=fork_id,
        valid=validation.valid,
        invalid_edges=validation.invalid_edges,
        draft_restored_from_id=draft_restored_from_id,
    )


@router.put("/{reservation_id}/canvas", response_model=ForkCanvasUpdateResponse)
async def update_fork_canvas_internal(
    reservation_id: uuid.UUID,
    body: ForkCanvasUpdate,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Loose draft edit: store the submitted canvas on the fork row only.

    This is the cheap-draft path (issue #25 P3a, ADR 0006 Decision 2). It runs NO
    save-reconcile of fork_connections and appends NO fork_versions row; those belong
    to the save endpoint (phase 2). It reuses the topology route validator to report
    route shape so the editor can flag unreachable edges, but does not gate the draft
    on it: an invalid draft still stores, matching "drafts are cheap". An ARCHIVED
    fork is frozen (Decision 5) and refuses the edit with 409. It never touches
    draft_restored_from_id either way (issue #622): editing a canvas that was just
    restored leaves the marker in place, since the user is still editing the restored
    draft and only a save consumes it.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")

    fork.canvas_data = body.canvas_data
    # _run_topology_validation reads only .canvas_data; the fork carries it, so hand
    # a detached probe with the new canvas rather than coupling to a Topology row.
    validation = await _run_topology_validation(Topology(canvas_data=body.canvas_data), db)
    fork_id = fork.id
    await db.commit()

    return ForkCanvasUpdateResponse(
        id=fork_id,
        valid=validation.valid,
        invalid_edges=validation.invalid_edges,
    )


@router.post("/{reservation_id}/save", response_model=ForkSaveResponse)
async def save_fork_internal(
    reservation_id: uuid.UUID,
    body: ForkSaveRequest,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Reconcile the fork's wiring against the submitted canvas and append a version.

    The heart of P3a (issue #25, ADR 0006 Decision 3 and 4). Diffs the resolved canvas
    against the fork's stored fork_connections by canonical connection identity and
    applies the delta release-before-build inside one transaction, enforcing
    cross-reservation port claims (409 naming the blocking reservation) and appending
    one fork_versions row. Rolls back wholesale on any failure: never a half-applied
    save. An ARCHIVED fork is frozen (Decision 5) and refuses the save with 409, the
    same wording pattern as the loose canvas PUT.

    Consumes the restore-to-draft marker (issue #622, see restore_fork_version_internal):
    if the fork's draft_restored_from_id is set, the appended fork_versions row carries
    it as its own restored_from_id, and the fork-row marker is cleared in the same
    transaction (see save_fork in fork_save_service.py). A save with no pending restore
    just appends restored_from_id=None as before.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")

    result = await save_fork(
        db,
        fork,
        canvas_data=body.canvas_data,
        created_by=body.created_by or "system",
    )

    return ForkSaveResponse(
        fork_id=result.fork_id,
        version_number=result.version_number,
        released=[_to_delta(spec) for spec in result.released],
        built=[_to_delta(spec) for spec in result.built],
        unchanged_count=result.unchanged_count,
        element_attachments_skipped=result.element_attachments_skipped,
    )


@router.post("/{reservation_id}/prune-devices", response_model=ForkPruneResponse)
async def prune_fork_devices_internal(
    reservation_id: uuid.UUID,
    body: ForkPruneRequest,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Release removed devices' wiring from the fork's saved intended set (issue #459).

    The device-set PATCH's REMOVE half (ADR 0009 Decision 6): reservations calls this
    when devices leave an ACTIVE reservation. Unlike the save endpoint this never
    reads the draft canvas as wiring intent: the release is computed from
    fork_connections plus the last SAVED canvas, the draft is only scrubbed of the
    removed devices (all other draft edits survive un-built), and the appended
    version snapshots the pruned SAVED canvas, never the draft. A pure release builds
    nothing, so no port-claim 409 is possible (issue #462); the only 409 is an
    ARCHIVED fork, whose release the terminal teardown owns. Idempotent: a replay
    releases nothing and reports changed false with no version appended.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")

    result = await prune_fork_devices(db, fork, body.device_ids)

    return ForkPruneResponse(
        fork_id=result.fork_id,
        version_number=result.version_number,
        changed=result.changed,
        released=[_to_delta(spec) for spec in result.released],
    )


@router.post("/{reservation_id}/archive", response_model=None)
async def archive_fork_internal(
    reservation_id: uuid.UUID,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
) -> Response | ForkArchiveResponse:
    """Freeze the fork as the immutable as-built record (issue #25 P3a, Decision 5).

    Flips status to ARCHIVED, the first actual use of ForkStatus_ARCHIVED, retaining
    all fork_connections and fork_versions read-only; every mutating endpoint then
    refuses the fork with 409. Appends no version: the as-built truth is the last saved
    state, and an unsaved draft was never built. Idempotent: archiving an already
    ARCHIVED fork returns 200 with the existing state, and archiving a nonexistent fork
    returns 204 (nothing to freeze), so a best-effort teardown call is safe to retry.
    """
    _check_internal_token(x_internal_token)
    fork = (
        await db.execute(
            select(ReservationFork).where(ReservationFork.reservation_id == reservation_id)
        )
    ).scalar_one_or_none()
    if fork is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if fork.status != ForkStatus_ARCHIVED:
        fork.status = ForkStatus_ARCHIVED
        await db.commit()

    return ForkArchiveResponse(
        fork_id=fork.id,
        reservation_id=fork.reservation_id,
        status=fork.status,
    )
