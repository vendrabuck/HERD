import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_payload
from app.schemas.pathfind import (
    PathfindBatchRequest,
    PathfindBatchResponse,
    PathfindBatchResult,
    PathfindRequest,
    PathfindResponse,
    PathHop,
)
from app.services.pathfind_service import (
    build_adjacency_graph,
    find_all_shortest_paths_async,
    find_all_shortest_paths_batch_async,
)
from app.services.visible_devices import resolve_caller_visibility

router = APIRouter(prefix="/pathfind", tags=["pathfind"])

# Wording shared by the single route's 404 and the batch route's per-pair
# ``error`` (issue #763). It is the same sentence an unknown device id gets,
# deliberately: a caller must not be able to tell "no such device" from "a
# device you cannot see", or the refusal itself becomes the oracle.
DEVICE_NOT_FOUND = "Device not found"

_VISIBILITY_UNAVAILABLE = (
    "Could not verify device visibility; no path was resolved. Retry the request."
)


def _redact_paths(
    paths: list[list[PathHop]],
    visible_ids: set[uuid.UUID],
) -> list[list[PathHop]]:
    """Blank out every hop through a device the caller cannot see (issue #763).

    A redacted hop keeps its POSITION (so hop_count, path count and
    reachability are exactly what an admin would see, which is all the editor
    and the Routes tab consume) and loses its identity: device_id null,
    hidden true, and both port names dropped, since a hidden device's port
    names are the other half of what the issue #719 connections filter refuses
    to disclose. Endpoint hops are never hidden here; a request whose own
    endpoint is invisible is refused before any path is resolved.
    """
    return [
        [
            hop
            if hop.device_id in visible_ids
            else PathHop(device_id=None, port_in=None, port_out=None, hidden=True)
            for hop in path
        ]
        for path in paths
    ]


@router.post("", response_model=PathfindResponse)
async def pathfind_endpoint(
    body: PathfindRequest,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Find every shortest physical cable path between two devices.

    The graph is scoped to the connected component(s) of the two endpoint
    devices. Component expansion still loads any intermediate hops on the route
    (a path is fully contained in its endpoints' component), so scoping cannot
    drop a valid path; it only skips unrelated isolated fabrics.

    Issue #763: for a NON-ADMIN caller an endpoint outside that caller's
    device-group visibility is refused with the same 404 an unknown device id
    gets (nothing is resolved, so reachability is not disclosed), and a transit
    hop through an invisible device comes back redacted. Admins are never
    filtered and never trigger the inventory lookup; the lookup fails CLOSED.
    """
    visible_ids = await resolve_caller_visibility(
        payload, authorization, unavailable_detail=_VISIBILITY_UNAVAILABLE
    )
    if visible_ids is not None and (
        body.source_device_id not in visible_ids or body.target_device_id not in visible_ids
    ):
        raise HTTPException(status_code=404, detail=DEVICE_NOT_FOUND)

    graph = await build_adjacency_graph(
        db, device_ids={body.source_device_id, body.target_device_id}
    )
    paths = await find_all_shortest_paths_async(graph, body.source_device_id, body.target_device_id)
    if not paths:
        return PathfindResponse(reachable=False, hop_count=0, paths=[])
    if visible_ids is not None:
        paths = _redact_paths(paths, visible_ids)
    return PathfindResponse(reachable=True, hop_count=len(paths[0]), paths=paths)


@router.post("/batch", response_model=PathfindBatchResponse)
async def pathfind_batch_endpoint(
    body: PathfindBatchRequest,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
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

    Issue #763: for a NON-ADMIN caller, a pair naming a device outside that
    caller's visibility is refused per pair (unreachable, ``error`` set to the
    single route's 404 wording) rather than failing the batch, and its
    endpoints never reach the graph seed, so nothing about it is resolved.
    Hidden transit hops on the surviving pairs are redacted exactly as in the
    single route.
    """
    visible_ids = await resolve_caller_visibility(
        payload, authorization, unavailable_detail=_VISIBILITY_UNAVAILABLE
    )

    def _refused(pair: PathfindRequest) -> bool:
        return visible_ids is not None and (
            pair.source_device_id not in visible_ids or pair.target_device_id not in visible_ids
        )

    # Refused pairs are dropped before the graph seed, so an invisible device
    # is never even expanded into the adjacency load. `allowed_indices` maps
    # each resolved result back onto its slot in the request order.
    allowed_indices = [idx for idx, pair in enumerate(body.pairs) if not _refused(pair)]

    device_ids: set[uuid.UUID] = set()
    for idx in allowed_indices:
        device_ids.add(body.pairs[idx].source_device_id)
        device_ids.add(body.pairs[idx].target_device_id)
    graph = await build_adjacency_graph(db, device_ids=device_ids)

    pair_tuples = [
        (body.pairs[idx].source_device_id, body.pairs[idx].target_device_id)
        for idx in allowed_indices
    ]
    per_pair_paths = await find_all_shortest_paths_batch_async(graph, pair_tuples)
    paths_by_index = dict(zip(allowed_indices, per_pair_paths))

    results: list[PathfindBatchResult] = []
    for idx, pair in enumerate(body.pairs):
        if idx not in paths_by_index:
            results.append(
                PathfindBatchResult(
                    source_device_id=pair.source_device_id,
                    target_device_id=pair.target_device_id,
                    reachable=False,
                    hop_count=0,
                    paths=[],
                    error=DEVICE_NOT_FOUND,
                )
            )
            continue
        paths = paths_by_index[idx]
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
            if visible_ids is not None:
                paths = _redact_paths(paths, visible_ids)
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
