"""Internal fork lifecycle endpoints (issue #25, Phase 2).

Reservations is the lifecycle authority and calls cabling at activation to create
the editable per-reservation fork. All endpoints here are service-to-service and
guarded by X-Internal-Token exactly like validate_topology_internal: the booking
user does not necessarily own the parent topology, so a JWT-forward would 403.

See docs/design/0001-editable-reservation-topologies.md (Decision 2).
"""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.fork import ForkVersion
from app.schemas.fork import ForkCreate, ForkCreateResponse
from app.services.fork_service import create_fork

router = APIRouter(prefix="/internal/forks", tags=["forks"])


def _check_internal_token(token: str) -> None:
    if not settings.internal_api_token or token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")


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
    )

    version_number = (
        await db.execute(
            select(func.max(ForkVersion.version_number)).where(ForkVersion.fork_id == fork.id)
        )
    ).scalar() or 1

    return ForkCreateResponse(fork_id=fork.id, version_number=version_number)
