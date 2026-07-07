import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_payload
from app.schemas.pathfind import (
    PathfindBatchRequest,
    PathfindBatchResponse,
    PathfindBatchResult,
    PathfindRequest,
    PathfindResponse,
)
from app.services.pathfind_service import (
    build_adjacency_graph,
    find_all_shortest_paths_async,
    find_all_shortest_paths_batch_async,
)

router = APIRouter(prefix="/pathfind", tags=["pathfind"])


@router.post("", response_model=PathfindResponse)
async def pathfind_endpoint(
    body: PathfindRequest,
    _: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """Find every shortest physical cable path between two devices.

    The graph is scoped to the connected component(s) of the two endpoint
    devices. Component expansion still loads any intermediate hops on the route
    (a path is fully contained in its endpoints' component), so scoping cannot
    drop a valid path; it only skips unrelated isolated fabrics.
    """
    graph = await build_adjacency_graph(
        db, device_ids={body.source_device_id, body.target_device_id}
    )
    paths = await find_all_shortest_paths_async(graph, body.source_device_id, body.target_device_id)
    if not paths:
        return PathfindResponse(reachable=False, hop_count=0, paths=[])
    return PathfindResponse(reachable=True, hop_count=len(paths[0]), paths=paths)


@router.post("/batch", response_model=PathfindBatchResponse)
async def pathfind_batch_endpoint(
    body: PathfindBatchRequest,
    _: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """Resolve many pathfind pairs against one adjacency graph build.

    Same semantics as the single endpoint, per pair: the graph is scoped to
    the connected component(s) of every requested endpoint device, built ONCE,
    and each pair is resolved via in-memory BFS. Results preserve request
    order and each entry carries the exact shape of the single pathfind
    response (an unreachable pair is reachable=false, hop_count=0, paths=[]),
    plus an echo of the requested pair. The pair-list length is capped at
    MAX_BATCH_PAIRS (422 beyond, enforced by the request schema); an empty
    list returns an empty results list.
    """
    device_ids: set[uuid.UUID] = set()
    for pair in body.pairs:
        device_ids.add(pair.source_device_id)
        device_ids.add(pair.target_device_id)
    graph = await build_adjacency_graph(db, device_ids=device_ids)

    pair_tuples = [(p.source_device_id, p.target_device_id) for p in body.pairs]
    per_pair_paths = await find_all_shortest_paths_batch_async(graph, pair_tuples)

    results: list[PathfindBatchResult] = []
    for pair, paths in zip(body.pairs, per_pair_paths):
        if not paths:
            results.append(
                PathfindBatchResult(
                    source_device_id=pair.source_device_id,
                    target_device_id=pair.target_device_id,
                    reachable=False,
                    hop_count=0,
                    paths=[],
                )
            )
        else:
            results.append(
                PathfindBatchResult(
                    source_device_id=pair.source_device_id,
                    target_device_id=pair.target_device_id,
                    reachable=True,
                    hop_count=len(paths[0]),
                    paths=paths,
                )
            )
    return PathfindBatchResponse(results=results)
