"""Internal fork lifecycle endpoints (issue #25, Phase 2).

Reservations is the lifecycle authority and calls cabling at activation to create
the editable per-reservation fork. All endpoints here are service-to-service and
guarded by X-Internal-Token exactly like validate_topology_internal: the booking
user does not necessarily own the parent topology, so a JWT-forward would 403.

See docs/design/0001-editable-reservation-topologies.md (Decision 2).
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.fork import ForkConnection, ForkStatus_ARCHIVED, ForkVersion, ReservationFork
from app.models.topology import Topology
from app.routes.topologies import _run_topology_validation
from app.schemas.fork import (
    ForkArchiveResponse,
    ForkCanvasUpdate,
    ForkCanvasUpdateResponse,
    ForkConnectionDelta,
    ForkConnectionResponse,
    ForkCreate,
    ForkCreateResponse,
    ForkDetailResponse,
    ForkSaveRequest,
    ForkSaveResponse,
    ForkVersionSummary,
)
from app.services.fork_save_service import save_fork
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
        created_at=fork.created_at,
        updated_at=fork.updated_at,
        connections=[ForkConnectionResponse.model_validate(c) for c in connections],
        versions=[ForkVersionSummary.model_validate(v) for v in versions],
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
    fork is frozen (Decision 5) and refuses the edit with 409.
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
        released=[
            ForkConnectionDelta(
                device_a_id=spec.device_a_id,
                port_a=spec.port_a,
                device_b_id=spec.device_b_id,
                port_b=spec.port_b,
                layer=spec.layer,
            )
            for spec in result.released
        ],
        built=[
            ForkConnectionDelta(
                device_a_id=spec.device_a_id,
                port_a=spec.port_a,
                device_b_id=spec.device_b_id,
                port_b=spec.port_b,
                layer=spec.layer,
            )
            for spec in result.built
        ],
        unchanged_count=result.unchanged_count,
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
