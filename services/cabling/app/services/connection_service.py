import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.connection import Connection
from app.schemas.connection import ConnectionCreate
from app.services.device_group_guard import DeviceNotFoundError, fetch_device_group_ids

logger = logging.getLogger(__name__)


async def _enforce_device_group_boundary(body: ConnectionCreate, bearer_token: str | None) -> None:
    """Reject cabling two devices whose device-group sets are disjoint.

    Allows the connection when either device is ungrouped (no boundary to
    enforce) or shares at least one group. Fail-open if inventory cannot be
    reached, so an inventory hiccup does not block admin cabling. A device
    that inventory confirms does not exist (404) is always a hard reject on
    this path: existence, unlike group membership, is not an optional boundary.
    No-op unless enforcement is enabled (matching the pre-existing gate; the
    existence check only runs where this guard already made an inventory
    call). See issue #392.
    """
    if not settings.enforce_device_group_boundaries:
        return
    try:
        a_groups = await fetch_device_group_ids(body.device_a_id, bearer_token or "")
        b_groups = await fetch_device_group_ids(body.device_b_id, bearer_token or "")
    except DeviceNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Device {exc.device_id} does not exist",
        ) from exc
    if a_groups is None or b_groups is None:
        logger.warning(
            "device_group_boundary_unverified",
            extra={"device_a_id": str(body.device_a_id), "device_b_id": str(body.device_b_id)},
        )
        return
    if a_groups and b_groups and a_groups.isdisjoint(b_groups):
        raise HTTPException(
            status_code=422,
            detail=(
                "Devices belong to different device groups and share none; "
                "cross-group cabling is disabled."
            ),
        )


async def create_connection(
    db: AsyncSession,
    body: ConnectionCreate,
    created_by: str,
    bearer_token: str | None = None,
) -> Connection:
    if body.device_a_id == body.device_b_id and body.port_a == body.port_b:
        raise HTTPException(
            status_code=422,
            detail="Cannot connect a port to itself",
        )
    await _enforce_device_group_boundary(body, bearer_token)
    conn = Connection(
        device_a_id=body.device_a_id,
        port_a=body.port_a,
        device_b_id=body.device_b_id,
        port_b=body.port_b,
        connection_type=body.connection_type,
        notes=body.notes,
        created_by=created_by,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def list_connections(
    db: AsyncSession,
    device_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Connection], int]:
    query = select(Connection)
    if device_id is not None:
        query = query.where(
            or_(
                Connection.device_a_id == device_id,
                Connection.device_b_id == device_id,
            )
        )

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        query.order_by(Connection.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all()), total


async def get_connection(db: AsyncSession, connection_id: uuid.UUID) -> Connection | None:
    return await db.get(Connection, connection_id)


async def delete_connection(db: AsyncSession, connection_id: uuid.UUID) -> bool:
    conn = await db.get(Connection, connection_id)
    if conn is None:
        return False
    await db.delete(conn)
    await db.commit()
    return True
