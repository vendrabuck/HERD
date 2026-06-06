from __future__ import annotations

import logging
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.auth import make_auth_dependencies
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.execution_run import ExecutionRun
from app.schemas.command_log import CommandLogEntry
from app.schemas.execution import (
    DeviceCheckRequest,
    DeviceCheckResponse,
    ExecutionRunResponse,
    ManualExecuteRequest,
    PaginatedExecutionRunResponse,
)
from app.services.execution_service import (
    fetch_device,
    fetch_template,
    get_execution_run,
    list_command_log,
    list_execution_runs,
    run_driver_action,
)

logger = logging.getLogger(__name__)

get_current_user_payload, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(tags=["executions"])


def _require_internal_token(x_internal_token: str = Header(None)) -> None:
    if not settings.internal_api_token:
        raise HTTPException(status_code=500, detail="Internal API token not configured")
    if x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")


async def _user_has_acl_manage(
    user_id: str, device_id: uuid.UUID, authorization: str | None
) -> bool:
    """Ask the ACL service whether the caller has manage on this device.

    Returns False on any failure (network error, non-2xx response, malformed
    payload). The /execute admin path is the safety net so a closed-by-default
    failure here is the right policy.
    """
    if not authorization:
        return False
    url = f"{settings.acl_service_url.rstrip('/')}/check"
    body = {
        "user_id": str(user_id),
        "resource_type": "device",
        "resource_id": str(device_id),
        "permission": "manage",
    }
    headers = {"Authorization": authorization}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("allowed", False))
    except ValueError:
        return False


async def _user_owns_reservation(reservation_id: uuid.UUID, authorization: str | None) -> bool:
    """Ask the reservations service whether the caller can read this reservation.

    The reservations service returns 200 to owners and admins, 404 to everyone
    else (non-owners and non-existent IDs share the 404 to avoid leaking
    existence). Returns False on any failure, mirroring `_user_has_acl_manage`.
    """
    if not authorization:
        return False
    url = f"{settings.reservations_service_url.rstrip('/')}/{reservation_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"Authorization": authorization})
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def _authorize_runs_list(
    reservation_id: uuid.UUID | None = Query(None),
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
) -> dict:
    """Authorize GET /runs. Admins always pass; non-admins must supply a
    reservation_id they own. Without a reservation_id, non-admins are rejected
    (the unscoped listing remains admin-only).
    """
    role = payload.get("role")
    if role in ("admin", "superadmin"):
        return payload
    if reservation_id is None:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not await _user_owns_reservation(reservation_id, authorization):
        raise HTTPException(status_code=403, detail="Reservation not owned by caller")
    return payload


async def _authorize_run_read(
    run: ExecutionRun,
    payload: dict,
    authorization: str | None,
) -> None:
    """Admin always passes; non-admins pass iff the run is tied to a reservation they own.

    Same shape as the iter-2 carve-out on GET /runs: reservation-owner reads
    are allowed when scoped to a reservation they hold. Runs with no
    reservation_id (e.g. ad-hoc device checks) remain admin-only.
    """
    role = payload.get("role")
    if role in ("admin", "superadmin"):
        return
    if run.reservation_id is None:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not await _user_owns_reservation(run.reservation_id, authorization):
        raise HTTPException(status_code=403, detail="Reservation not owned by caller")


# --- API Endpoints ---


@router.get("/runs", response_model=PaginatedExecutionRunResponse)
async def list_runs(
    device_id: uuid.UUID | None = Query(None),
    reservation_id: uuid.UUID | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _: dict = Depends(_authorize_runs_list),
    db: AsyncSession = Depends(get_db),
):
    """List execution runs. Admin (any filter) or reservation owner (with reservation_id)."""
    items, total = await list_execution_runs(
        db,
        device_id=device_id,
        reservation_id=reservation_id,
        status_filter=status_filter,
        created_after=created_after,
        created_before=created_before,
        skip=skip,
        limit=limit,
    )
    return PaginatedExecutionRunResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("/runs/{run_id}", response_model=ExecutionRunResponse)
async def get_run(
    run_id: uuid.UUID,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get execution run detail. Admin only."""
    run = await get_execution_run(db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Execution run not found")
    return run


@router.get("/runs/{run_id}/commands", response_model=list[CommandLogEntry])
async def list_run_commands(
    run_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """List the per-command transcript for an execution run.

    Admin always. Non-admin reservation owners may read transcripts for runs
    tied to a reservation they hold (matches the iter-2 carve-out on GET /runs).
    Returns an empty list for runs whose driver did not opt into command
    logging; the absence of rows is a property of the driver, not an error.
    """
    run = await get_execution_run(db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Execution run not found")
    await _authorize_run_read(run, payload, authorization)
    return await list_command_log(db, run_id)


@router.post("/device-check", response_model=DeviceCheckResponse)
async def device_check(
    body: DeviceCheckRequest,
    _: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a status check on a device. Internal token auth."""
    device_data = await fetch_device(body.device_id)
    template_data = await fetch_template(device_data["template_id"])

    # Execute login -> status -> logout
    login_run = await run_driver_action(db, device_data, template_data, "login", body.user_id)
    if login_run.status != "SUCCESS":
        return DeviceCheckResponse(
            run_id=login_run.id,
            device_id=body.device_id,
            status="FAILED",
            error=login_run.error,
        )

    status_run = await run_driver_action(db, device_data, template_data, "status", body.user_id)

    await run_driver_action(db, device_data, template_data, "logout", body.user_id)

    return DeviceCheckResponse(
        run_id=status_run.id,
        device_id=body.device_id,
        status=status_run.status,
        output=status_run.output,
        error=status_run.error,
    )


@router.post("/execute", response_model=ExecutionRunResponse, status_code=status.HTTP_201_CREATED)
async def manual_execute(
    body: ManualExecuteRequest,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Manually execute a driver action.

    Admins may execute any action. Non-admins are allowed iff:
      - action == "configure", AND
      - the caller has an ACL `manage` grant on this device.

    All other non-admin calls are rejected 403.
    """
    role = payload.get("role", "user")
    if role not in ("admin", "superadmin"):
        if body.action != "configure":
            raise HTTPException(status_code=403, detail="Admin access required")
        allowed = await _user_has_acl_manage(payload["sub"], body.device_id, authorization)
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="Admin access or device manage grant required",
            )

    device_data = await fetch_device(body.device_id)
    template_data = await fetch_template(device_data["template_id"])

    # Audit attribution comes from the JWT subject, NOT body.user_id. Without
    # this override, a caller could set body.user_id to anyone's UUID and frame
    # them in the execution_runs audit trail. body.user_id is left in the
    # schema for backwards compatibility with internal_execute (where it IS the
    # source of truth, trusted via INTERNAL_API_TOKEN).
    auditing_user_id = uuid.UUID(payload["sub"])
    run = await run_driver_action(
        db,
        device_data,
        template_data,
        body.action,
        auditing_user_id,
        reservation_id=body.reservation_id,
        port_a=body.port_a,
        port_b=body.port_b,
        method_kwargs=body.method_kwargs,
        dry_run=body.dry_run,
    )
    return run


@router.post(
    "/execute/internal",
    response_model=ExecutionRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def internal_execute(
    body: ManualExecuteRequest,
    _: None = Depends(_require_internal_token),
    db: AsyncSession = Depends(get_db),
):
    """Service-to-service execute endpoint (no user JWT).

    Used by the inventory scheduler to fire scheduled config-apply jobs. The
    INTERNAL_API_TOKEN header guards this path; access auditing relies on
    `body.user_id` (the original job creator) being recorded on the run.

    The action is locked to "configure" so this carve-out cannot be used to
    invoke arbitrary driver methods (delete, reboot, etc.) that the regular
    JWT-protected path would gate behind admin RBAC. Any other action returns
    422; the caller must use the user-facing /execute endpoint instead.
    """
    if body.action != "configure":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"internal execute is restricted to action='configure'; got '{body.action}'"),
        )
    device_data = await fetch_device(body.device_id)
    template_data = await fetch_template(device_data["template_id"])
    return await run_driver_action(
        db,
        device_data,
        template_data,
        body.action,
        body.user_id,
        reservation_id=body.reservation_id,
        port_a=body.port_a,
        port_b=body.port_b,
        method_kwargs=body.method_kwargs,
        dry_run=body.dry_run,
    )


@router.post("/runs/{run_id}/retry", response_model=ExecutionRunResponse)
async def retry_run(
    run_id: uuid.UUID,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retry a failed execution run. Admin only."""
    original = await get_execution_run(db, run_id)
    if not original:
        raise HTTPException(status_code=404, detail="Execution run not found")
    if original.status not in ("FAILED", "TIMEOUT"):
        raise HTTPException(status_code=400, detail="Only failed or timed-out runs can be retried")

    device_data = await fetch_device(original.device_id)
    template_data = await fetch_template(device_data["template_id"])

    # Recover method_kwargs from the original run's input_params
    original_kwargs = (original.input_params or {}).get("method_kwargs")

    run = await run_driver_action(
        db,
        device_data,
        template_data,
        original.action,
        original.user_id,
        reservation_id=original.reservation_id,
        port_a=original.port_a,
        port_b=original.port_b,
        method_kwargs=original_kwargs,
    )
    return run
