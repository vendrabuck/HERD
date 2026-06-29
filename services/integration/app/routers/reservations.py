"""Versioned v1 reservation facade.

A thin, stable external API that forwards the caller's JWT to the internal
reservations service. RBAC and device-group visibility are enforced unchanged by
the downstream service; this facade only validates that a JWT is present and valid,
freezes the v1 request/response contract, and propagates upstream status codes.

The app is mounted under /api/v1 by Traefik (which strips the prefix), so these
routes carry no extra prefix and the app sees /reservations.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from herd_common.auth import make_auth_dependencies
from httpx import AsyncClient

from app.config import settings
from app.schemas.reservation import (
    V1ReservationList,
    V1ReservationRequest,
    V1ReservationResponse,
)

logger = logging.getLogger(__name__)

get_current_user_payload, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(tags=["v1-reservations"])
bearer_scheme = HTTPBearer()

# Short timeout: the facade is a synchronous hop in front of reservations; a slow
# upstream should surface as a 503 rather than hang the external caller.
UPSTREAM_TIMEOUT = 10.0


async def _forward(
    method: str,
    path: str,
    *,
    token: str,
    json: Any | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Forward a request to the reservations service, carrying the caller's JWT.

    Forwarding the bearer token (rather than an internal token) is what lets the
    downstream service enforce RBAC and device-group visibility as the real user.
    On a transport failure (upstream unreachable or timed out), raise 503.
    """
    url = f"{settings.reservations_service_url}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            return await client.request(method, url, headers=headers, json=json, params=params)
    except httpx.RequestError:
        logger.warning("reservations service unreachable for %s %s", method, path)
        raise HTTPException(status_code=503, detail="Reservations service unavailable")


def _propagate_if_error(resp: httpx.Response) -> None:
    """Re-raise an upstream non-2xx as the same status code to the external caller.

    A 403/404/409/422 from reservations becomes the same to the v1 client, so the
    facade is transparent for error semantics. The detail is taken from the upstream
    JSON `detail` when present, else the raw body.
    """
    if resp.is_success:
        return
    detail: Any
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        detail = resp.text or "Upstream error"
    raise HTTPException(status_code=resp.status_code, detail=detail)


def _to_v1(data: dict[str, Any]) -> V1ReservationResponse:
    """Translate an internal reservation payload into the frozen v1 view."""
    return V1ReservationResponse(
        id=data["id"],
        status=data["status"],
        device_ids=data["device_ids"],
        topology_id=data.get("topology_id"),
        start_time=data["start_time"],
        end_time=data["end_time"],
        created_at=data["created_at"],
    )


@router.post(
    "/reservations",
    response_model=V1ReservationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_reservation(
    body: V1ReservationRequest,
    _payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Reserve devices. Forwards the translated body to reservations as the caller."""
    upstream_body = {
        "device_ids": [str(d) for d in body.device_ids],
        "start_time": body.start_time.isoformat(),
        "end_time": body.end_time.isoformat(),
        "purpose": body.purpose,
        "topology_id": str(body.topology_id) if body.topology_id else None,
    }
    resp = await _forward("POST", "/", token=credentials.credentials, json=upstream_body)
    _propagate_if_error(resp)
    return _to_v1(resp.json())


@router.get("/reservations", response_model=V1ReservationList)
async def list_reservations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """List the caller's reservations (visibility enforced downstream)."""
    resp = await _forward(
        "GET",
        "/",
        token=credentials.credentials,
        params={"skip": skip, "limit": limit},
    )
    _propagate_if_error(resp)
    data = resp.json()
    return V1ReservationList(
        items=[_to_v1(item) for item in data.get("items", [])],
        total=data.get("total", 0),
        skip=data.get("skip", skip),
        limit=data.get("limit", limit),
    )


@router.get("/reservations/{reservation_id}", response_model=V1ReservationResponse)
async def get_reservation(
    reservation_id: str,
    _payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Fetch one reservation's status (ownership/visibility enforced downstream)."""
    resp = await _forward("GET", f"/{reservation_id}", token=credentials.credentials)
    _propagate_if_error(resp)
    return _to_v1(resp.json())


@router.delete("/reservations/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reservation(
    reservation_id: str,
    _payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Cancel a reservation."""
    resp = await _forward("DELETE", f"/{reservation_id}", token=credentials.credentials)
    _propagate_if_error(resp)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/reservations/{reservation_id}/release", response_model=V1ReservationResponse)
async def release_reservation(
    reservation_id: str,
    _payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    """Release a reservation early (frees its devices before the scheduled end)."""
    resp = await _forward("PUT", f"/{reservation_id}/release", token=credentials.credentials)
    _propagate_if_error(resp)
    return _to_v1(resp.json())
