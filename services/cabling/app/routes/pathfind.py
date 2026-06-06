from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_payload
from app.schemas.pathfind import PathfindRequest, PathfindResponse
from app.services.pathfind_service import build_adjacency_graph, find_all_shortest_paths

router = APIRouter(prefix="/pathfind", tags=["pathfind"])


@router.post("", response_model=PathfindResponse)
async def pathfind_endpoint(
    body: PathfindRequest,
    _: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """Find every shortest physical cable path between two devices."""
    graph = await build_adjacency_graph(db)
    paths = find_all_shortest_paths(graph, body.source_device_id, body.target_device_id)
    if not paths:
        return PathfindResponse(reachable=False, hop_count=0, paths=[])
    return PathfindResponse(reachable=True, hop_count=len(paths[0]), paths=paths)
