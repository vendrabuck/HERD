import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_payload
from app.models.topology import Topology, TopologyVersion
from app.schemas.topology import (
    PaginatedTopologyVersionResponse,
    TopologyDetail,
    TopologyRestoreRequest,
    TopologyVersionDetail,
    TopologyVersionDiff,
)
from app.services.canvas_nodes import strip_device_nodes
from app.services.reservation_guard import find_blocking_reservations
from app.services.version_diff import diff_canvas
from app.services.version_service import commit_with_new_version

router = APIRouter(prefix="/topologies/{topology_id}/versions", tags=["topology-versions"])


async def _load_topology(db: AsyncSession, topology_id: uuid.UUID) -> Topology:
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")
    return topology


async def _load_version(
    db: AsyncSession, topology_id: uuid.UUID, version_id: uuid.UUID
) -> TopologyVersion:
    version = await db.get(TopologyVersion, version_id)
    if not version or version.topology_id != topology_id:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


def _require_mutator(topology: Topology, payload: dict) -> None:
    role = payload.get("role", "user")
    if str(topology.created_by) != payload["sub"] and role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Not authorized to modify this topology")


@router.get("", response_model=PaginatedTopologyVersionResponse)
async def list_versions(
    topology_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    await _load_topology(db, topology_id)

    total = (
        await db.execute(
            select(func.count())
            .select_from(TopologyVersion)
            .where(TopologyVersion.topology_id == topology_id)
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                select(TopologyVersion)
                .where(TopologyVersion.topology_id == topology_id)
                .order_by(TopologyVersion.version_number.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return PaginatedTopologyVersionResponse(
        items=list(rows),
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/diff", response_model=TopologyVersionDiff)
async def diff_versions(
    topology_id: uuid.UUID,
    a: uuid.UUID = Query(...),
    b: uuid.UUID = Query(...),
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    await _load_topology(db, topology_id)
    version_a = await _load_version(db, topology_id, a)
    version_b = await _load_version(db, topology_id, b)
    # Read-side strip (belt and braces, see get_topology): diff_canvas echoes
    # whole node dicts verbatim into nodes_added/nodes_removed/nodes_modified,
    # so a pre-fix row's device node would otherwise leak field_data straight
    # into this diff. Strip both sides before diffing, never after: stripping
    # post-diff would still leave nodes_modified's stale "before"/"after"
    # values dirty since diff_collection copies the dicts it indexes.
    diff = diff_canvas(
        strip_device_nodes(version_a.canvas_data), strip_device_nodes(version_b.canvas_data)
    )
    return TopologyVersionDiff(version_a=a, version_b=b, **diff)


@router.get("/{version_id}", response_model=TopologyVersionDetail)
async def get_version(
    topology_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    await _load_topology(db, topology_id)
    version = await _load_version(db, topology_id, version_id)
    # Read-side strip (belt and braces, see get_topology): build the response
    # model explicitly and overwrite its canvas_data rather than mutate the
    # ORM object, so this read never persists what it strips.
    detail = TopologyVersionDetail.model_validate(version)
    detail.canvas_data = strip_device_nodes(detail.canvas_data)
    return detail


@router.post(
    "/{version_id}/restore",
    response_model=TopologyDetail,
    status_code=status.HTTP_200_OK,
)
async def restore_version(
    topology_id: uuid.UUID,
    version_id: uuid.UUID,
    body: TopologyRestoreRequest,
    request: Request,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await _load_topology(db, topology_id)
    _require_mutator(topology, payload)
    version = await _load_version(db, topology_id, version_id)

    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token:
        blocking = await find_blocking_reservations(topology_id)
        if blocking:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Topology has active reservations; restore blocked",
                    "reservations": [
                        {
                            "id": str(b.get("id")),
                            "status": b.get("status"),
                            "end_time": b.get("end_time"),
                        }
                        for b in blocking
                    ],
                },
            )

    # version.canvas_data was already stripped when that version was written;
    # strip again anyway (cheap, idempotent) since restoring is a write
    # boundary of its own, copying the version's canvas back onto the live
    # topology and into a fresh version snapshot.
    restored_canvas = strip_device_nodes(version.canvas_data)
    topology.canvas_data = restored_canvas
    if body.restore_name:
        topology.name = version.name
    topology.modified_by = uuid.UUID(payload["sub"])

    description = body.description or f"Restored from v{version.version_number}"
    # version_number is allocated as max+1 under a unique constraint; serialize
    # against concurrent writers with a bounded retry loop rather than risking a
    # raw IntegrityError 500 (see commit_with_new_version).
    snapshot = TopologyVersion(
        topology_id=topology.id,
        canvas_data=restored_canvas,
        name=topology.name,
        description=description,
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
        restored_from_id=version.id,
    )
    await commit_with_new_version(db, topology, snapshot)
    await db.refresh(topology)
    return topology
