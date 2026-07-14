import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.models.reservation import Reservation, ReservationDevice, ReservationStatus
from app.schemas.reservation import (
    OwnsActiveResponse,
    PaginatedReservationResponse,
    ProvisionResultRequest,
    ProvisionResultResponse,
    ReservationCreate,
    ReservationInternalStatus,
    ReservationResponse,
    ReservationUpdate,
    UtilizationReport,
)
from app.services.reporting_service import (
    build_utilization_report,
    fetch_execution_run_count,
    fetch_fleet_devices,
    fetch_user_groups_map,
    report_to_csv,
    rollup_by_group,
)
from app.services.reservation_service import (
    apply_provision_result,
    cancel_reservation,
    create_reservation,
    get_reservation,
    list_calendar_reservations,
    list_user_reservations,
    release_reservation,
    update_reservation,
)

logger = logging.getLogger(__name__)

# Default reservation statuses for the fleet-utilization section. Includes
# ACTIVE (unlike the legacy sections' COMPLETED-only default) so a window
# touching the present does not under-report in-flight usage.
FLEET_DEFAULT_STATUS_FILTER = [ReservationStatus.ACTIVE, ReservationStatus.COMPLETED]

router = APIRouter(tags=["reservations"])
bearer_scheme = HTTPBearer()


@router.post("/", response_model=ReservationResponse, status_code=status.HTTP_201_CREATED)
async def create_new_reservation(
    body: ReservationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    user_id = uuid.UUID(payload["sub"])
    username = payload.get("username", "")
    role = payload.get("role", "user")

    # Non-admin users can only reserve visible devices
    if role not in ("admin", "superadmin"):
        visible = await _fetch_visible_device_ids(user_id, credentials.credentials)
        if visible is not None:
            invisible = [str(d) for d in body.device_ids if str(d) not in visible]
            if invisible:
                raise HTTPException(
                    status_code=403,
                    detail="You do not have access to one or more requested devices",
                )

    try:
        reservation = await create_reservation(
            db, body, user_id, credentials.credentials, username=username
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return reservation


@router.get("/", response_model=PaginatedReservationResponse)
async def get_my_reservations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = uuid.UUID(payload["sub"])
    reservations, total = await list_user_reservations(db, user_id, skip=skip, limit=limit)
    return PaginatedReservationResponse(
        items=reservations,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/calendar", response_model=list[ReservationResponse])
async def get_calendar_reservations(
    range_start: datetime = Query(...),
    range_end: datetime = Query(...),
    status: list[ReservationStatus] | None = Query(None),
    device_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    role = payload.get("role", "user")
    visible_device_ids = None
    if role not in ("admin", "superadmin"):
        user_id = uuid.UUID(payload["sub"])
        visible = await _fetch_visible_device_ids(user_id, credentials.credentials)
        if visible is not None:
            visible_device_ids = visible

    try:
        return await list_calendar_reservations(
            db,
            range_start=range_start,
            range_end=range_end,
            status_filter=status,
            device_id=device_id,
            visible_device_ids=visible_device_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/reports/utilization", response_model=UtilizationReport)
async def get_utilization_report(
    request: Request,
    start: datetime = Query(...),
    end: datetime = Query(...),
    status: list[ReservationStatus] | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    status_filter = status if status else [ReservationStatus.COMPLETED]
    # The fleet section defaults to ACTIVE+COMPLETED so a reservation running
    # right now counts toward utilization; an explicit ?status= applies to
    # every section so a caller-chosen filter stays internally consistent.
    fleet_filter = status if status else FLEET_DEFAULT_STATUS_FILTER
    auth_header = request.headers.get("Authorization")
    fleet_devices = await fetch_fleet_devices(auth_header)
    try:
        report = await build_utilization_report(
            db,
            start,
            end,
            status_filter,
            fleet_status_filter=fleet_filter,
            fleet_devices=fleet_devices,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    report.execution_run_count = await fetch_execution_run_count(start, end, auth_header)
    user_ids = [b.user_id for b in report.by_user]
    user_groups = await fetch_user_groups_map(user_ids, auth_header)
    report.by_group = rollup_by_group(report.by_user, user_groups)
    return report


@router.get("/reports/utilization.csv")
async def get_utilization_report_csv(
    request: Request,
    start: datetime = Query(...),
    end: datetime = Query(...),
    section: str = Query("user", pattern="^(user|device|fleet)$"),
    status: list[ReservationStatus] | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    status_filter = status if status else [ReservationStatus.COMPLETED]
    fleet_devices = None
    fleet_filter = None
    if section == "fleet":
        # Only the fleet section needs the inventory join; skip the HTTP
        # round-trip for the other sections.
        fleet_filter = status if status else FLEET_DEFAULT_STATUS_FILTER
        fleet_devices = await fetch_fleet_devices(request.headers.get("Authorization"))
        if fleet_devices is None:
            raise HTTPException(status_code=503, detail="Inventory service is unreachable")
    try:
        report = await build_utilization_report(
            db,
            start,
            end,
            status_filter,
            fleet_status_filter=fleet_filter,
            fleet_devices=fleet_devices,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    body = report_to_csv(report, section)
    filename = f"utilization-{section}-{start.date().isoformat()}-to-{end.date().isoformat()}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/internal/active", response_model=OwnsActiveResponse)
async def owns_active_reservation_for_device(
    user_id: uuid.UUID = Query(...),
    device_id: uuid.UUID = Query(...),
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Does this user own an ACTIVE reservation containing this device?

    Internal-token-guarded service-to-service endpoint. Used by inventory's
    widened ACL helper so a reservation owner without an explicit `manage`
    grant on a device can still author and schedule configs against devices
    in their own currently-active reservation. Returns owns_active=False on
    any failure or absence; closed-by-default.

    Device membership is an indexed EXISTS over reservation_devices; the
    time-window check stays in Python so naive timestamps from the SQLite
    test backend are normalized to UTC consistently with Postgres.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Reservation).where(
            Reservation.user_id == user_id,
            Reservation.status == ReservationStatus.ACTIVE,
            exists().where(
                and_(
                    ReservationDevice.reservation_id == Reservation.id,
                    ReservationDevice.device_id == device_id,
                )
            ),
        )
    )
    for reservation in result.scalars():
        start = reservation.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = reservation.end_time
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start <= now <= end:
            return OwnsActiveResponse(owns_active=True)
    return OwnsActiveResponse(owns_active=False)


@router.get("/internal/active-users", response_model=list[uuid.UUID])
async def list_active_reservation_users_for_device(
    device_id: uuid.UUID = Query(...),
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> list[uuid.UUID]:
    """Return user_ids of every active reservation that includes this device.

    Used by notifications (ROADMAP #13 iter 2) to fan out device health
    transition events to anyone currently using the device. Device membership
    is an indexed EXISTS over reservation_devices; the time-window check stays
    in Python so naive SQLite timestamps normalize to UTC consistently.

    Deduplicates: a user with multiple active reservations on the same
    device appears once.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Reservation).where(
            Reservation.status == ReservationStatus.ACTIVE,
            exists().where(
                and_(
                    ReservationDevice.reservation_id == Reservation.id,
                    ReservationDevice.device_id == device_id,
                )
            ),
        )
    )
    user_ids: set[uuid.UUID] = set()
    for reservation in result.scalars():
        start = reservation.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = reservation.end_time
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start <= now <= end:
            user_ids.add(reservation.user_id)
    return list(user_ids)


@router.get("/internal/by-topology/{topology_id}")
async def list_reservations_for_topology(
    topology_id: uuid.UUID,
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Return reservations referencing a topology, unfiltered by device visibility.

    Internal-token-guarded service-to-service endpoint used by the cabling
    service's reservation lock. The user-facing /calendar route is visibility-
    filtered for non-admins, so a non-admin topology creator cannot see (and
    would not be blocked by) a reservation held by another user on devices they
    cannot see. This endpoint sees every reservation on the topology so the lock
    holds across visibility boundaries. Declared before /internal/{reservation_id}
    so the literal "by-topology" segment is not parsed as a reservation id.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    result = await db.execute(select(Reservation).where(Reservation.topology_id == topology_id))
    return [
        {
            "id": str(r.id),
            "user_id": str(r.user_id),
            "topology_id": str(r.topology_id) if r.topology_id else None,
            "status": r.status.value,
            "end_time": r.end_time.isoformat(),
        }
        for r in result.scalars()
    ]


@router.get("/internal/{reservation_id}", response_model=ReservationInternalStatus)
async def get_reservation_internal_status(
    reservation_id: uuid.UUID,
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Internal status lookup for service-to-service callers (e.g., apply scheduler).

    Guarded by X-Internal-Token. Returns status + is_active boolean (status ACTIVE
    AND within window) so callers do not need to replicate active-window logic.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    reservation = await db.get(Reservation, reservation_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    # SQLite (test backend) drops tzinfo; Postgres preserves it. Treat naive as UTC.
    start = reservation.start_time
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = reservation.end_time
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    is_active = reservation.status == ReservationStatus.ACTIVE and start <= now <= end
    return ReservationInternalStatus(
        id=reservation.id,
        status=reservation.status,
        is_active=is_active,
        start_time=start,
        end_time=end,
    )


@router.post("/internal/{reservation_id}/provision-result", response_model=ProvisionResultResponse)
async def post_provision_result(
    reservation_id: uuid.UUID,
    body: ProvisionResultRequest,
    x_internal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Provision-result callback from the execution service (ADR 0004, issue #32).

    Guarded by X-Internal-Token. Idempotent per reservation: only a reservation
    still in PENDING_PROVISION transitions (success activates and stages
    reservation.created; failure lands FAILED and stages reservation.failed). A
    duplicate or late callback returns 200 with applied=False and never
    resurrects a reservation the timeout backstop or a user cancel already
    moved on.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    reservation, applied = await apply_provision_result(
        db,
        reservation_id,
        succeeded=body.succeeded,
        device_ids=body.device_ids,
        error=body.error,
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return ProvisionResultResponse(
        reservation_id=reservation.id,
        status=reservation.status,
        applied=applied,
    )


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation_by_id(
    reservation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = uuid.UUID(payload["sub"])
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation


@router.patch("/{reservation_id}", response_model=ReservationResponse)
async def update_reservation_by_id(
    reservation_id: uuid.UUID,
    body: ReservationUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Update a reservation (end_time, purpose, device_ids). Owner only, ACTIVE/PENDING only."""
    user_id = uuid.UUID(payload["sub"])
    role = payload.get("role", "user")

    # Non-admin users can only add visible devices
    if body.device_ids is not None and role not in ("admin", "superadmin"):
        visible = await _fetch_visible_device_ids(user_id, credentials.credentials)
        if visible is not None:
            invisible = [str(d) for d in body.device_ids if str(d) not in visible]
            if invisible:
                raise HTTPException(
                    status_code=403,
                    detail="You do not have access to one or more requested devices",
                )

    try:
        reservation = await update_reservation(
            db,
            reservation_id,
            user_id,
            body,
            token=credentials.credentials,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation


@router.delete("/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reservation_by_id(
    reservation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    user_id = uuid.UUID(payload["sub"])
    reservation = await cancel_reservation(
        db, reservation_id, user_id, token=credentials.credentials
    )
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")


@router.put("/{reservation_id}/release", response_model=ReservationResponse)
async def release_reservation_early(
    reservation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    user_id = uuid.UUID(payload["sub"])
    reservation = await release_reservation(
        db, reservation_id, user_id, token=credentials.credentials
    )
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation


async def _fetch_visible_device_ids(user_id: uuid.UUID, token: str) -> set[str] | None:
    """Fetch visible device IDs for a user from the inventory service.

    Forwards the caller's JWT: the inventory /device-groups/visible-devices
    endpoint is JWT-guarded (not internal-token), so the bearer token is what
    authenticates this call and lets inventory resolve the user's groups as the
    real user. Returns None on a genuine transport error (fail-open, do not
    restrict); a None from here means the visibility filter is skipped.

    This fail-open is deliberate and intentionally differs from the 503 raised
    when the create-reservation device-info fetch fails (see create_reservation,
    which surfaces a hard RuntimeError -> 503). Visibility is an additive filter:
    losing it on a transient inventory outage degrades to "show/allow more,"
    never to a privilege escalation across a tenancy boundary, so blocking the
    request would be a worse outcome than briefly skipping the filter. The
    device-info fetch, by contrast, is load-bearing for correctness, so it fails
    closed. Keep the two behaviors distinct on purpose.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.inventory_service_url}/device-groups/visible-devices",
                params={"user_id": str(user_id)},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return set(data.get("device_ids", []))
            logger.warning("visible-devices returned %s for user %s", resp.status_code, user_id)
    except Exception:
        logger.warning("Failed to fetch visible devices for user %s", user_id)
    return None
