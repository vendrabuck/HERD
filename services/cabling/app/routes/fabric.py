import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.fabric import FabricResponse
from app.services.fabric_service import compute_fabric_id, find_connected_component
from app.services.pathfind_service import build_adjacency_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fabric", tags=["fabric"])


@router.get("/internal", response_model=FabricResponse)
async def get_fabric_internal(
    device_id: uuid.UUID = Query(..., description="Device to look up fabric membership for"),
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Return the fabric (connected component) a device belongs to.

    Internal service-to-service endpoint, guarded by token.
    """
    if not settings.internal_api_token or x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")

    graph = await build_adjacency_graph(db)
    component = find_connected_component(graph, device_id)
    fabric_id = compute_fabric_id(component)

    return FabricResponse(
        device_id=device_id,
        fabric_id=fabric_id,
        component_size=len(component),
    )
