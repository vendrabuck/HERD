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
    ForkL3Route,
    ForkStatus_ACTIVE,
    ForkStatus_ARCHIVED,
    ForkVersion,
    ReservationFork,
)
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
    ForkDevicesBatchRequest,
    ForkDevicesBatchResponse,
    ForkL3RouteResponse,
    ForkPruneRequest,
    ForkPruneResponse,
    ForkRestoreResponse,
    ForkSaveRequest,
    ForkSaveResponse,
    ForkVersionDetailResponse,
    ForkVersionSummary,
)
from app.services.fork_save_service import (
    WireSpec,
    assert_endpoints_are_members,
    gate_l3_intent,
    l3_intent_changed,
    prune_fork_devices,
    resolve_canvas_wiring,
    save_fork,
    touched_devices_from_specs,
)
from app.services.fork_service import create_fork
from app.services.l3_intent import merge_candidates_by_device, walk_l3_nodes
from app.services.topology_validation import validate_canvas_edges

router = APIRouter(prefix="/internal/forks", tags=["forks"])


def _check_internal_token(token: str) -> None:
    if not internal_token_matches(token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")


async def _load_fork(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    for_update: bool = False,
    refresh: bool = False,
) -> ReservationFork:
    """Load the fork row for a reservation, optionally under a row lock.

    Issue #626: restore and save both read-modify-write draft_restored_from_id
    and canvas_data with no coordination, so a restore that commits between a
    save's read and its commit is silently overwritten by the save's stale
    in-memory copy. The invariant is that a restore marker is either consumed by
    exactly one appended fork_versions row or still present on the fork row,
    never lost. ``for_update=True`` takes ``FOR UPDATE`` on the row (SQLAlchemy
    only emits it on dialects that support it, so this is a no-op on the SQLite
    engine the unit suites use); every writer that passes it must hold the lock
    from this load through its own final commit or rollback, not release and
    reacquire mid-request. GET routes never pass it: they only read.

    ``refresh=True`` (S1 review fix, round 2 on 2ade362c) adds
    ``populate_existing=True`` to the SELECT's execution options. Proven live by
    the review: a caller that already loaded this row once in the SAME session
    (``save_fork_internal``'s unlocked pre-gate load, re-loaded ``for_update``
    after the gate) gets back the SAME Python object from SQLAlchemy's identity
    map by default, with whatever column values it had at the FIRST load, even
    though the second SELECT's ``FOR UPDATE`` genuinely re-reads and locks the
    row at the database. Without this, a concurrent archive or restore that
    committed between the two loads is invisible to the caller despite the lock
    having just proven the row's current committed state. Every caller that
    re-loads a fork it already holds in this session within one request must
    pass this; a caller's first load in a request needs it only if another
    reference to the same row could already be memoized in that session (none
    of the current single-load routes are).
    """
    stmt = select(ReservationFork).where(ReservationFork.reservation_id == reservation_id)
    if for_update:
        stmt = stmt.with_for_update()
    if refresh:
        stmt = stmt.execution_options(populate_existing=True)
    fork = (await db.execute(stmt)).scalar_one_or_none()
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
        member_device_ids=set(body.member_device_ids),
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


@router.post("/devices/batch", response_model=ForkDevicesBatchResponse)
async def get_fork_devices_batch_internal(
    body: ForkDevicesBatchRequest,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Devices a batch of reservations consumed, for transit-gear reporting (#646 P3).

    For each reservation_id that has a fork (ACTIVE or ARCHIVED), returns the
    sorted distinct union of device_a_id/device_b_id across all of its
    fork_connections rows, both layers. A reservation with no fork (never
    activated, or dynamic-only) is absent from the map; the caller (reservations'
    utilization report) treats absence as "no path data" and reports zero transit
    devices for it. One join query, no per-reservation loop, so the 500-id cap on
    the request keeps this to a single bounded round trip regardless of how many
    reservations a report window covers.
    """
    _check_internal_token(x_internal_token)

    rows = (
        await db.execute(
            select(
                ReservationFork.reservation_id,
                ForkConnection.device_a_id,
                ForkConnection.device_b_id,
            )
            .join(ForkConnection, ForkConnection.fork_id == ReservationFork.id)
            .where(ReservationFork.reservation_id.in_(body.reservation_ids))
        )
    ).all()

    devices_by_reservation: dict[uuid.UUID, set[uuid.UUID]] = {}
    for reservation_id, device_a_id, device_b_id in rows:
        bucket = devices_by_reservation.setdefault(reservation_id, set())
        bucket.add(device_a_id)
        bucket.add(device_b_id)

    return ForkDevicesBatchResponse(
        devices={
            str(reservation_id): sorted(device_ids, key=str)
            for reservation_id, device_ids in devices_by_reservation.items()
        }
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
    # ADR 0014 phase 1 (issue #34): the fork's resolved L3 routing intent, sorted
    # by (device_id, route_key) so the response is deterministic regardless of
    # insertion order.
    l3_routes = (
        (
            await db.execute(
                select(ForkL3Route)
                .where(ForkL3Route.fork_id == fork.id)
                .order_by(ForkL3Route.device_id, ForkL3Route.route_key)
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
        l3_routes=[ForkL3RouteResponse.model_validate(r) for r in l3_routes],
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

    Loads the fork row FOR UPDATE (issue #626): without a lock, a save racing this
    restore between its own load and its own commit can overwrite this restore's
    fresh marker with the save's stale, already-cleared copy, silently losing it.
    Holding the row lock from this load through commit serializes against a
    concurrent save's own locked load.

    Runs ONLY the edge pass (``validate_canvas_edges``, R1 review fix on
    2ade362c), never the L3 pass: this endpoint's contract is "the draft stores
    regardless", so it must never make an inventory call or 503 on an unsaved
    draft. ``valid``/``invalid_edges`` are therefore edge-only, unchanged in
    shape and behavior from before ADR 0014.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id, for_update=True)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")
    version = await _load_fork_version(db, fork.id, version_id)

    restored_canvas = version.canvas_data
    fork.canvas_data = restored_canvas
    fork.draft_restored_from_id = version.id
    # Same validation the loose canvas PUT runs, on the same terms: reported, not
    # gated on. This also mirrors that endpoint in appending no version and not
    # touching fork_connections.
    edge_validation = await validate_canvas_edges(restored_canvas, db)
    fork_id = fork.id
    draft_restored_from_id = fork.draft_restored_from_id
    await db.commit()

    return ForkRestoreResponse(
        id=fork_id,
        valid=not edge_validation.invalid_edges,
        invalid_edges=edge_validation.invalid_edges,
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

    Loads the fork row FOR UPDATE (issue #626): this writes canvas_data after a
    plain load same as the other mutators, so it is locked for consistency even
    though it never touches draft_restored_from_id and the canvas itself stays
    last-writer-wins by design.

    Runs ONLY the edge pass (``validate_canvas_edges``, R1 review fix on
    2ade362c), never the L3 pass, for the same "the draft stores regardless"
    reason ``restore_fork_version_internal`` documents: no inventory call, no 503,
    on an unsaved draft.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id, for_update=True)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")

    fork.canvas_data = body.canvas_data
    edge_validation = await validate_canvas_edges(body.canvas_data, db)
    fork_id = fork.id
    await db.commit()

    return ForkCanvasUpdateResponse(
        id=fork_id,
        valid=not edge_validation.invalid_edges,
        invalid_edges=edge_validation.invalid_edges,
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

    Loads the fork row FOR UPDATE (issue #626): without a lock, this save can read
    the marker, decide to clear it, and then commit after a concurrent restore has
    set a fresh one, dropping the restore's marker on the floor. The lock is held
    from this load through the final commit inside commit_fork_with_new_version's
    retry loop, serializing against a concurrent restore's own locked load.

    Lock ordering (R3 review fix on 2ade362c): the fork row is loaded WITHOUT
    ``FOR UPDATE`` first, just to check ARCHIVED before doing any real work; the
    canvas is then resolved (``resolve_canvas_wiring``) and, when needed (S5),
    the L3 intent gated (``gate_l3_intent``), BOTH of which can make inventory
    HTTP calls, entirely before any lock is taken. Only after those calls return
    does the fork get re-loaded WITH ``FOR UPDATE`` (re-checking ARCHIVED, since
    the fork's status could have changed in the gap) and handed to ``save_fork``
    along with the already-resolved wiring and already-gated intent, so the
    version-allocation retry loop's reapply covers only the set arithmetic,
    never a second resolve, gate, or inventory round trip while the row is
    locked. The locked re-load passes ``refresh=True`` (S1 review fix, round 2):
    without it, SQLAlchemy's identity map hands back the FIRST load's stale
    column values even though the ``FOR UPDATE`` genuinely re-reads and locks
    the row, so a concurrent archive or restore committed in the gap would be
    invisible to the ARCHIVED re-check and the restore-marker read alike.

    Membership is checked BEFORE the gate (S3 review fix, round 2): a canvas
    naming a device outside the reservation is refused with 409
    ``fork_device_not_member`` and no inventory lookup at all, rather than
    paying for (and potentially leaking a reason through) an L3 validation call
    against a device this reservation has no business naming. ``save_fork``'s
    own ``reconcile()`` still runs the same check again on every version-race
    retry, since the retry reapplies against freshly-read state.

    The canvas is parsed exactly ONCE, via ``l3_intent.walk_l3_nodes`` (S10
    review fix, round 2): a malformed node 422s immediately (no validation call,
    no inventory call), and the well-formed candidates feed both the merged
    ``intended_routes`` (for the reconcile) and, when needed, ``gate_l3_intent``
    (for validation), so the canvas's ``data.l3`` is never re-parsed.

    The L3 validation pass (and its inventory calls) runs ONLY when the intent
    actually changed (S5 review fix, round 2): ``l3_intent_changed`` compares
    the parsed intent against the fork's EXISTING ``ForkL3Route`` rows (route-key
    sets per device) before any lock is taken; when they match, no
    ``resolve_canvas_wiring``-derived touched-device set is even needed for L3
    purposes and no inventory call is made, so a wiring-only save on a fork
    whose routes were already validated when written cannot 503. See ADR 0014's
    phase 1 review-fixes amendment for the accepted consequence.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")

    member_device_ids = set(body.member_device_ids)
    assert_endpoints_are_members(body.canvas_data, member_device_ids)

    candidates, malformed = walk_l3_nodes(body.canvas_data)
    if malformed:
        _node_id, _device_id, exc = malformed[0]
        raise HTTPException(
            status_code=422,
            detail={
                "error": "l3_intent_malformed",
                "node_id": exc.node_id,
                "message": exc.message,
            },
        )
    intended_routes = merge_candidates_by_device(candidates)

    wiring_resolution = await resolve_canvas_wiring(db, body.canvas_data)

    validated_config_version_ids: dict[uuid.UUID, uuid.UUID | None] = {}
    if intended_routes:
        existing_l3_rows = (
            (await db.execute(select(ForkL3Route).where(ForkL3Route.fork_id == fork.id)))
            .scalars()
            .all()
        )
        if l3_intent_changed(existing_l3_rows, intended_routes):
            touched_devices = touched_devices_from_specs(wiring_resolution.specs)
            validated_config_version_ids = await gate_l3_intent(candidates, touched_devices)

    fork = await _load_fork(db, reservation_id, for_update=True, refresh=True)
    if fork.status == ForkStatus_ARCHIVED:
        raise HTTPException(status_code=409, detail="Fork is archived and cannot be edited")

    result = await save_fork(
        db,
        fork,
        canvas_data=body.canvas_data,
        member_device_ids=member_device_ids,
        wiring_resolution=wiring_resolution,
        intended_routes=intended_routes,
        validated_config_version_ids=validated_config_version_ids,
        created_by=body.created_by or "system",
    )

    return ForkSaveResponse(
        fork_id=result.fork_id,
        version_number=result.version_number,
        released=[_to_delta(spec) for spec in result.released],
        built=[_to_delta(spec) for spec in result.built],
        unchanged_count=result.unchanged_count,
        element_attachments_skipped=result.element_attachments_skipped,
        l3_routes_built=result.l3_routes_built,
        l3_routes_released=result.l3_routes_released,
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

    Loads the fork row FOR UPDATE (issue #626): this appends a fork_versions row
    through the same commit_fork_with_new_version retry loop as save, so an
    unlocked prune racing a locked save could still force save into a
    version-conflict retry; the retry rolls back and reapplies save's own captured
    field values without reacquiring the lock, which reopens the exact window a
    concurrent restore could land in. Locking every writer that can append a
    fork_versions row removes the only source of that retry for a shared fork, so
    the retry path is effectively unreachable here in normal operation.
    """
    _check_internal_token(x_internal_token)
    fork = await _load_fork(db, reservation_id, for_update=True)
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
