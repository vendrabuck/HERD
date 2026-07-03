import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.hypervisor import Hypervisor
from app.schemas.hypervisor import (
    HypervisorCreate,
    HypervisorInternalResponse,
    HypervisorResponse,
    HypervisorUpdate,
    PaginatedHypervisorResponse,
)
from app.services.hypervisor_service import (
    create_hypervisor,
    delete_hypervisor,
    get_hypervisor,
    list_hypervisors,
    update_hypervisor,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hypervisors"])


def _to_response(hypervisor: Hypervisor) -> HypervisorResponse:
    return HypervisorResponse.model_validate(hypervisor)


@router.get("/hypervisors", response_model=PaginatedHypervisorResponse)
async def get_hypervisors(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """List registered hypervisors. Admin or superadmin only."""
    hypervisors, total = await list_hypervisors(db, skip=skip, limit=limit)
    return PaginatedHypervisorResponse(
        items=[_to_response(h) for h in hypervisors],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/hypervisors",
    response_model=HypervisorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_new_hypervisor(
    body: HypervisorCreate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Register a hypervisor. Admin or superadmin only. Validates the secret exists."""
    hypervisor = await create_hypervisor(db, body, modified_by=uuid.UUID(payload["sub"]))
    logger.info(
        "Hypervisor created: %s",
        hypervisor.name,
        extra={"action": "hypervisor_create", "hypervisor_id": str(hypervisor.id)},
    )
    return _to_response(hypervisor)


@router.get("/hypervisors/{hypervisor_id}/internal", response_model=HypervisorInternalResponse)
async def get_hypervisor_internal(
    hypervisor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Get a hypervisor for service-to-service use, guarded by the internal token."""
    if not settings.internal_api_token or x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")
    hypervisor = await get_hypervisor(db, hypervisor_id)
    if not hypervisor:
        raise HTTPException(status_code=404, detail="Hypervisor not found")
    return HypervisorInternalResponse.model_validate(hypervisor)


@router.get("/hypervisors/{hypervisor_id}", response_model=HypervisorResponse)
async def get_hypervisor_by_id(
    hypervisor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Get a single hypervisor. Admin or superadmin only."""
    hypervisor = await get_hypervisor(db, hypervisor_id)
    if not hypervisor:
        raise HTTPException(status_code=404, detail="Hypervisor not found")
    return _to_response(hypervisor)


@router.put("/hypervisors/{hypervisor_id}", response_model=HypervisorResponse)
async def update_hypervisor_by_id(
    hypervisor_id: uuid.UUID,
    body: HypervisorUpdate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Update a hypervisor. Admin or superadmin only. Re-validates a changed secret."""
    hypervisor = await update_hypervisor(
        db, hypervisor_id, body, modified_by=uuid.UUID(payload["sub"])
    )
    if not hypervisor:
        raise HTTPException(status_code=404, detail="Hypervisor not found")
    logger.info(
        "Hypervisor updated: %s",
        hypervisor_id,
        extra={"action": "hypervisor_update", "hypervisor_id": str(hypervisor_id)},
    )
    return _to_response(hypervisor)


@router.delete("/hypervisors/{hypervisor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hypervisor_by_id(
    hypervisor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Delete a hypervisor. Admin or superadmin only. Fails if templates reference it."""
    deleted = await delete_hypervisor(db, hypervisor_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Hypervisor not found")
    logger.info(
        "Hypervisor deleted: %s",
        hypervisor_id,
        extra={"action": "hypervisor_delete", "hypervisor_id": str(hypervisor_id)},
    )
