import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user_payload, require_admin
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionResponse,
    PaginatedConnectionResponse,
)
from app.services.connection_service import (
    create_connection,
    delete_connection,
    get_connection,
    list_connections,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("", response_model=PaginatedConnectionResponse)
async def list_connections_endpoint(
    device_id: uuid.UUID | None = Query(None, description="Filter by device on either side"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """List all backend connections. Available to all authenticated users (read-only)."""
    items, total = await list_connections(db, device_id=device_id, skip=skip, limit=limit)
    return PaginatedConnectionResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("/internal", response_model=PaginatedConnectionResponse)
async def list_connections_internal(
    device_id: uuid.UUID | None = Query(None, description="Filter by device on either side"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """List connections. Internal service-to-service endpoint, guarded by token."""
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    items, total = await list_connections(db, device_id=device_id, skip=skip, limit=limit)
    return PaginatedConnectionResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection_endpoint(
    connection_id: uuid.UUID,
    _: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """Get a single backend connection. Available to all authenticated users."""
    conn = await get_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection_endpoint(
    body: ConnectionCreate,
    payload: dict = Depends(require_admin),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a backend connection between two device ports. Admin or superadmin only.
    This represents a physical cable or virtual link that end-users do not configure directly.
    """
    created_by = payload.get("username", "unknown")
    # Forward the caller's JWT so the device-group boundary check can read each
    # device's group membership from inventory as the real user.
    bearer_token = authorization.removeprefix("Bearer ").strip() if authorization else None
    conn = await create_connection(db, body, created_by, bearer_token=bearer_token)
    logger.info(
        "Connection created",
        extra={
            "connection_id": str(conn.id),
            "device_a_id": str(body.device_a_id),
            "device_b_id": str(body.device_b_id),
            "created_by": created_by,
        },
    )
    return conn


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection_endpoint(
    connection_id: uuid.UUID,
    payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a backend connection. Admin or superadmin only."""
    deleted = await delete_connection(db, connection_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found")
    logger.info(
        "Connection deleted",
        extra={
            "connection_id": str(connection_id),
            "deleted_by": payload.get("username", "unknown"),
        },
    )
