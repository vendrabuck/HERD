import asyncio
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


class _NotFoundMarker:
    """Sentinel cached in GroupCache for a device inventory confirmed does not exist.

    Earlier this cached and re-raised the SAME DeviceNotFoundError instance
    for every row that referenced the device. Re-raising one exception object
    repeatedly mutates its shared __traceback__ (each re-raise appends the
    new frame), so a device referenced by hundreds of bulk rows accumulated
    an unreadable, ever-growing traceback in the logs. This marker is only
    ever compared by identity; _cached_fetch_group_ids raises a fresh
    DeviceNotFoundError at each use site instead.
    """


_DEVICE_NOT_FOUND = _NotFoundMarker()

# Per-request/per-batch memoization of fetch_device_group_ids results, keyed by
# device id. A cached _DEVICE_NOT_FOUND marker means a fresh DeviceNotFoundError
# is raised at each use; a cached None or set is returned as-is. This lets a
# batch of many rows sharing a small set of devices pay for each device's
# inventory lookup once instead of once per row. See create_connections_bulk.
GroupCache = dict[uuid.UUID, set[str] | None | _NotFoundMarker]


async def _cached_fetch_group_ids(
    device_id: uuid.UUID, bearer_token: str, cache: GroupCache
) -> set[str] | None:
    """Look up one device's group ids through the shared cache.

    A cache hit never makes an HTTP call. The single-create path builds this
    cache lazily (a miss triggers a real fetch here), so the two lookups in
    one row still get memoized against each other. The bulk path pre-resolves
    the whole cache up front (see _resolve_group_cache_for_batch) before the
    validation loop runs, so during that loop this function only ever hits
    the cache branch below and issues no HTTP calls of its own.
    """
    if device_id in cache:
        cached = cache[device_id]
        if cached is _DEVICE_NOT_FOUND:
            raise DeviceNotFoundError(device_id)
        return cached  # type: ignore[return-value]
    try:
        result = await fetch_device_group_ids(device_id, bearer_token)
    except DeviceNotFoundError:
        cache[device_id] = _DEVICE_NOT_FOUND
        raise
    cache[device_id] = result
    return result


async def _resolve_group_cache_for_batch(
    items: list[ConnectionCreate], bearer_token: str
) -> GroupCache:
    """Resolve every distinct device id referenced in a bulk batch, concurrently.

    One fetch_device_group_ids call per distinct device id (not per row, not
    per side), run together with asyncio.gather(return_exceptions=True) so
    one slow or failing lookup cannot cancel or serialize behind the others.
    A worst-case 200-row batch of all-distinct devices used to make up to 400
    sequential calls at a 5s timeout each; this makes at most 400 calls
    concurrently instead.

    The caller (create_connections_bulk) treats a resolved None anywhere in
    the returned cache as fail-CLOSED for the whole batch: unlike the
    single-create path, which stays fail-open, a bulk write is a larger
    deliberate action, so refusing it costs the admin one retry, versus
    silently creating up to 200 cables that violate the device-group policy
    because one transient inventory blip suppressed the boundary check for
    every remaining row. This mirrors the fail-closed fork fetch from issue
    #460, and 503 is the codebase's existing status for a fail-closed
    upstream dependency (see inventory's device-delete guard).
    """
    device_ids = sorted(
        {item.device_a_id for item in items} | {item.device_b_id for item in items},
        key=str,
    )
    results = await asyncio.gather(
        *(fetch_device_group_ids(device_id, bearer_token) for device_id in device_ids),
        return_exceptions=True,
    )
    cache: GroupCache = {}
    for device_id, result in zip(device_ids, results, strict=True):
        if isinstance(result, DeviceNotFoundError):
            cache[device_id] = _DEVICE_NOT_FOUND
        elif isinstance(result, BaseException):
            # fetch_device_group_ids already swallows transport/non-404 errors
            # internally and returns None; a BaseException surfacing here
            # (e.g. asyncio-level) is treated the same way: unverifiable, not
            # confirmed-gone.
            logger.warning(
                "device_group_guard_unexpected_error",
                extra={"device_id": str(device_id), "error": str(result)},
            )
            cache[device_id] = None
        else:
            cache[device_id] = result
    return cache


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
    memoized against each other but not across separate connections. The bulk
    path (create_connections_bulk) pre-resolves its whole cache up front and
    aborts the entire batch on any unverifiable device before this function
    ever runs a row through it, so the None fail-open branch below is live
    only for the single-create path in practice.
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

    When device-group enforcement is on, every distinct device id referenced
    anywhere in the batch is resolved concurrently up front (see
    _resolve_group_cache_for_batch) before any row is validated, so the
    per-row loop below reads only from that cache and makes no HTTP calls of
    its own. If membership could not be verified for ANY device in the batch,
    the whole request is aborted with 503 and nothing is created or
    committed: see that helper's docstring for why bulk fails closed here
    while the single-create path still fails open.
    """
    group_cache: GroupCache = {}
    if settings.enforce_device_group_boundaries:
        group_cache = await _resolve_group_cache_for_batch(items, bearer_token or "")
        if any(value is None for value in group_cache.values()):
            unverifiable = sorted(str(k) for k, v in group_cache.items() if v is None)
            logger.warning(
                "bulk_device_group_boundary_unverifiable",
                extra={"device_ids": unverifiable, "item_count": len(items)},
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not verify device-group membership for one or more "
                    "devices in this batch; no connections were created. Retry "
                    "the request."
                ),
            )

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
        # The session is created with expire_on_commit=False (app/database.py),
        # so each pending object's server-generated id is already populated
        # right after commit; only conn.id is read below. db.refresh() here
        # would just re-SELECT every row for no observable benefit, up to 200
        # discarded queries for a full batch. create_connection (single-create)
        # still refreshes because it returns the full row to its caller,
        # including server-generated fields like created_at that this bulk
        # path never reads.
        for conn in pending:
            logger.info(
                "Bulk connection created",
                extra={
                    "connection_id": str(conn.id),
                    "device_a_id": str(conn.device_a_id),
                    "port_a": conn.port_a,
                    "device_b_id": str(conn.device_b_id),
                    "port_b": conn.port_b,
                    "created_by": created_by,
                },
            )

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
