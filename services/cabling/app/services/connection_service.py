import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.connection import Connection
from app.schemas.connection import ConnectionBulkReport, ConnectionBulkRowResult, ConnectionCreate
from app.services.device_group_guard import DeviceNotFoundError, fetch_device_group_ids

logger = logging.getLogger(__name__)

# Per-request/per-batch memoization of fetch_device_group_ids results, keyed by
# device id. A cached DeviceNotFoundError instance is re-raised (never
# consumed); a cached None or set is returned as-is. This lets a batch of many
# rows sharing a small set of devices pay for each device's inventory lookup
# once instead of once per row. See create_connections_bulk.
GroupCache = dict[uuid.UUID, set[str] | None | DeviceNotFoundError]


async def _cached_fetch_group_ids(
    device_id: uuid.UUID, bearer_token: str, cache: GroupCache
) -> set[str] | None:
    if device_id in cache:
        cached = cache[device_id]
        if isinstance(cached, DeviceNotFoundError):
            raise cached
        return cached
    try:
        result = await fetch_device_group_ids(device_id, bearer_token)
    except DeviceNotFoundError as exc:
        cache[device_id] = exc
        raise
    cache[device_id] = result
    return result


async def _enforce_device_group_boundary(
    body: ConnectionCreate,
    bearer_token: str | None,
    group_cache: GroupCache | None = None,
) -> None:
    """Reject cabling two devices whose device-group sets are disjoint.

    Allows the connection when either device is ungrouped (no boundary to
    enforce) or shares at least one group. Fail-open if inventory cannot be
    reached, so an inventory hiccup does not block admin cabling. A device
    that inventory confirms does not exist (404) is always a hard reject on
    this path: existence, unlike group membership, is not an optional boundary.
    No-op unless enforcement is enabled (matching the pre-existing gate; the
    existence check only runs where this guard already made an inventory
    call). See issue #392.

    group_cache memoizes fetch_device_group_ids by device id for the duration
    of the caller's request or batch. Callers that omit it (the single-create
    path) get a cache scoped to just this call, so the two lookups below are
    memoized against each other but not across separate connections.
    """
    if not settings.enforce_device_group_boundaries:
        return
    cache: GroupCache = group_cache if group_cache is not None else {}
    try:
        a_groups = await _cached_fetch_group_ids(body.device_a_id, bearer_token or "", cache)
        b_groups = await _cached_fetch_group_ids(body.device_b_id, bearer_token or "", cache)
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


async def _validate_connection_row(
    body: ConnectionCreate,
    bearer_token: str | None,
    group_cache: GroupCache | None = None,
) -> None:
    """Validate one connection row: self-loop then device-group boundary.

    Raises HTTPException on rejection. Shared by the single-create and bulk
    paths so both apply exactly the same rules; the bulk path passes a
    group_cache shared across the whole batch, the single path does not.
    """
    if body.device_a_id == body.device_b_id and body.port_a == body.port_b:
        raise HTTPException(
            status_code=422,
            detail="Cannot connect a port to itself",
        )
    await _enforce_device_group_boundary(body, bearer_token, group_cache)


def _build_connection(body: ConnectionCreate, created_by: str) -> Connection:
    return Connection(
        device_a_id=body.device_a_id,
        port_a=body.port_a,
        device_b_id=body.device_b_id,
        port_b=body.port_b,
        connection_type=body.connection_type,
        notes=body.notes,
        created_by=created_by,
    )


async def create_connection(
    db: AsyncSession,
    body: ConnectionCreate,
    created_by: str,
    bearer_token: str | None = None,
) -> Connection:
    await _validate_connection_row(body, bearer_token)
    conn = _build_connection(body, created_by)
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def create_connections_bulk(
    db: AsyncSession,
    items: list[ConnectionCreate],
    created_by: str,
    bearer_token: str | None = None,
) -> ConnectionBulkReport:
    """Validate every row, then insert all valid rows and commit once.

    A row rejected by validation never blocks its siblings: rejections are
    collected per-row and the batch still creates everything that validates.
    Duplicate rows (including exact repeats) are not deduplicated; that is
    deliberate, matching the single-create endpoint's existing behavior.
    group_cache is shared across the whole batch, so fetch_device_group_ids is
    called at most once per distinct device id referenced in items, not once
    per row.
    """
    group_cache: GroupCache = {}
    outcomes: list[tuple[int, str, Connection | None, str | None]] = []
    pending: list[Connection] = []
    for index, item in enumerate(items):
        try:
            await _validate_connection_row(item, bearer_token, group_cache)
        except HTTPException as exc:
            outcomes.append((index, "rejected", None, str(exc.detail)))
            continue
        conn = _build_connection(item, created_by)
        db.add(conn)
        pending.append(conn)
        outcomes.append((index, "created", conn, None))

    if pending:
        await db.commit()
        for conn in pending:
            await db.refresh(conn)

    rows = [
        ConnectionBulkRowResult(
            index=index,
            status=row_status,
            connection_id=conn.id if conn is not None else None,
            error=error,
        )
        for index, row_status, conn, error in outcomes
    ]
    created = len(pending)
    return ConnectionBulkReport(created=created, rejected=len(items) - created, rows=rows)


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
