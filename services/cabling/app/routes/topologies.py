import copy
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user_payload
from app.models.topology import Topology, TopologyVersion
from app.schemas.topology import (
    InvalidEdge,
    PaginatedTopologyResponse,
    TopologyClone,
    TopologyCreate,
    TopologyDetail,
    TopologyUpdate,
    TopologyValidationResponse,
)
from app.services.pathfind_service import (
    build_adjacency_graph,
    find_all_shortest_paths_batch_async,
)
from app.services.reservation_guard import find_blocking_reservations
from app.services.version_service import commit_with_new_version

router = APIRouter(prefix="/topologies", tags=["topologies"])


@router.get("", response_model=PaginatedTopologyResponse)
async def list_topologies(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    count_query = select(func.count()).select_from(Topology)
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        select(Topology).order_by(Topology.updated_at.desc()).offset(skip).limit(limit)
    )
    topologies = result.scalars().all()
    return PaginatedTopologyResponse(
        items=list(topologies),
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post("", response_model=TopologyDetail, status_code=status.HTTP_201_CREATED)
async def create_topology(
    body: TopologyCreate,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = Topology(
        name=body.name,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
        canvas_data=None,
    )
    db.add(topology)
    await db.commit()
    await db.refresh(topology)
    return topology


@router.get("/{topology_id}", response_model=TopologyDetail)
async def get_topology(
    topology_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")
    return topology


@router.put("/{topology_id}", response_model=TopologyDetail)
async def update_topology(
    topology_id: uuid.UUID,
    body: TopologyUpdate,
    request: Request,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    # Only creator or admin can update
    user_role = payload.get("role", "user")
    is_admin = user_role in ("admin", "superadmin")
    if str(topology.created_by) != payload["sub"] and not is_admin:
        raise HTTPException(status_code=403, detail="Not authorized to update this topology")

    canvas_changed = body.canvas_data is not None and body.canvas_data != topology.canvas_data

    # Reservation-scoped lock: a topology with a live reservation may only have
    # its wiring changed by the reservation owner (or an admin). This is what
    # makes "edit the running reservation's topology" safe: the owner editing
    # their own live reservation is allowed and flows through to provisioning,
    # while an unrelated user cannot mutate wiring out from under an active
    # reservation. Name/description-only edits do not touch the live wiring, so
    # they are not gated. The guard fails open if the reservations service is
    # unreachable (see find_blocking_reservations).
    if canvas_changed:
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if token and not is_admin:
            blocking = await find_blocking_reservations(topology_id)
            others = [b for b in blocking if str(b.get("user_id") or "") != payload["sub"]]
            if others:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "Topology is in use by an active reservation owned by "
                            "another user; editing its wiring is blocked"
                        ),
                        "reservations": [
                            {
                                "id": str(b.get("id")),
                                "status": b.get("status"),
                                "end_time": b.get("end_time"),
                            }
                            for b in others
                        ],
                    },
                )

    if body.name is not None:
        topology.name = body.name
    if body.canvas_data is not None:
        topology.canvas_data = body.canvas_data
    topology.modified_by = uuid.UUID(payload["sub"])

    if canvas_changed:
        # version_number is allocated as max+1 under a unique constraint; serialize
        # against concurrent writers with a bounded retry loop rather than risking a
        # raw IntegrityError 500 (see commit_with_new_version).
        snapshot = TopologyVersion(
            topology_id=topology.id,
            canvas_data=body.canvas_data,
            name=topology.name,
            description=body.description,
            created_by=uuid.UUID(payload["sub"]),
            author_name=payload.get("username", ""),
        )
        await commit_with_new_version(db, topology, snapshot)
    else:
        await db.commit()
    await db.refresh(topology)
    return topology


@router.post(
    "/{topology_id}/clone",
    response_model=TopologyDetail,
    status_code=status.HTTP_201_CREATED,
)
async def clone_topology(
    topology_id: uuid.UUID,
    body: TopologyClone,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(Topology, topology_id)
    if not source:
        raise HTTPException(status_code=404, detail="Topology not found")

    cloned_canvas = None if source.canvas_data is None else copy.deepcopy(source.canvas_data)

    clone = Topology(
        name=body.name,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
        canvas_data=cloned_canvas,
    )
    db.add(clone)
    await db.flush()

    snapshot = TopologyVersion(
        topology_id=clone.id,
        version_number=1,
        canvas_data=cloned_canvas,
        name=clone.name,
        description=f"Cloned from {source.name}",
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    db.add(snapshot)

    await db.commit()
    await db.refresh(clone)
    return clone


async def _run_topology_validation(
    topology: Topology, db: AsyncSession
) -> TopologyValidationResponse:
    """Walk the topology canvas and check each edge against the cabling graph.

    Shared by the public /validate (user-facing) and /validate/internal (service)
    endpoints; the only thing that differs between the two is the auth path.
    """
    canvas = topology.canvas_data or {}
    nodes = canvas.get("nodes") or []
    edges = canvas.get("edges") or []

    # node_id (React Flow id) to device_id map. Edges reference React Flow node ids,
    # not device ids; we resolve devices through this map.
    node_to_device: dict[str, uuid.UUID] = {}
    for node in nodes:
        node_id = node.get("id")
        device_id_str = ((node.get("data") or {}).get("device") or {}).get("id")
        if not node_id or not device_id_str:
            continue
        try:
            node_to_device[node_id] = uuid.UUID(device_id_str)
        except (ValueError, TypeError):
            continue

    if not edges:
        return TopologyValidationResponse(valid=True, invalid_edges=[])

    # Scope the graph to the connected component(s) of the topology's devices.
    # Expansion still loads off-canvas intermediates (a patch panel that
    # physically realizes an edge), so no reachable path is dropped; it only
    # skips fabrics unrelated to this topology. Built once and reused across all
    # edges, as before.
    graph = await build_adjacency_graph(db, device_ids=set(node_to_device.values()))

    # First pass: skip proposal edges, classify missing-device edges immediately
    # (same reason and fields as before), and defer the rest for pathfinding.
    # `edge_results` is positional (one slot per edge in `edges`, None meaning
    # "not invalid" or "proposal, skipped"), so the final invalid list can be
    # reassembled in the exact original per-edge order regardless of how the
    # batched pathfind call below resolves its pairs. `pending` carries the
    # (edges-index, edge_id, layer, source_device, target_device) tuples for
    # every edge that needs a path lookup, in edge order.
    edge_results: list[InvalidEdge | None] = [None] * len(edges)
    pending: list[tuple[int, str, str | None, uuid.UUID, uuid.UUID]] = []

    for idx, edge in enumerate(edges):
        edge_id = str(edge.get("id") or "")
        edge_data = edge.get("data") or {}
        layer = edge_data.get("layer")
        # Skip proposal edges (not yet committed by the user).
        if edge_data.get("isProposal"):
            continue

        source_node = edge.get("source")
        target_node = edge.get("target")
        source_device = node_to_device.get(source_node) if source_node else None
        target_device = node_to_device.get(target_node) if target_node else None

        if source_device is None or target_device is None:
            edge_results[idx] = InvalidEdge(
                edge_id=edge_id,
                source_device_id=source_device,
                target_device_id=target_device,
                layer=layer,
                reason="missing_device",
            )
            continue

        pending.append((idx, edge_id, layer, source_device, target_device))

    # Second pass: one batched BFS call over every resolvable pair instead of
    # one asyncio.to_thread hop per edge (issue #313, same fix class as
    # #249/#250). Results are returned in request order, matching `pending`
    # positionally, and an empty path-list means the same "no_path" outcome
    # find_all_shortest_paths_async would have produced for that pair.
    pairs = [(source_device, target_device) for _, _, _, source_device, target_device in pending]
    all_paths = await find_all_shortest_paths_batch_async(graph, pairs)

    for (idx, edge_id, layer, source_device, target_device), paths in zip(pending, all_paths):
        if not paths:
            edge_results[idx] = InvalidEdge(
                edge_id=edge_id,
                source_device_id=source_device,
                target_device_id=target_device,
                layer=layer,
                reason="no_path",
            )

    invalid = [result for result in edge_results if result is not None]
    return TopologyValidationResponse(valid=not invalid, invalid_edges=invalid)


@router.post("/{topology_id}/validate/internal", response_model=TopologyValidationResponse)
async def validate_topology_internal(
    topology_id: uuid.UUID,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Service-to-service validate; X-Internal-Token only.

    Reservations calls this during create to gate bookings against topologies
    with unreachable edges. The booking user does not necessarily own the
    topology being reserved, so JWT-forward against the public /validate
    endpoint would 403 on the creator-or-admin check.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")
    return await _run_topology_validation(topology, db)


@router.post("/{topology_id}/validate", response_model=TopologyValidationResponse)
async def validate_topology(
    topology_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """User-facing validate: returns invalid edges for the topology editor.

    RBAC: validation reveals which device pairs lack physical paths, so it is
    restricted to the topology creator or admins. Service-to-service callers
    use /validate/internal instead.
    """
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    user_role = payload.get("role", "user")
    if str(topology.created_by) != payload["sub"] and user_role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Not authorized to validate this topology")

    return await _run_topology_validation(topology, db)


@router.delete("/{topology_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topology(
    topology_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    # Only creator or admin can delete
    user_role = payload.get("role", "user")
    if str(topology.created_by) != payload["sub"] and user_role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Not authorized to delete this topology")

    await db.delete(topology)
    await db.commit()
