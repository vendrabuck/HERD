"""Cross-service ACL helpers.

`user_has_manage_or_owns_active_reservation` consolidates the duplicated
`_user_has_acl_manage` helper that lived in three places (inventory's
device_configs and apply_jobs routers, execution's executions router) and
adds the reservation-owner free pass: a caller who owns a currently-active
reservation containing the device satisfies `manage` for the purposes of
config-version writes and apply-job scheduling.

Closed-by-default: any failure path (network error, non-2xx, malformed
JSON, missing token) returns False so the caller cannot accidentally
authorize a request.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_ACL_HTTP_TIMEOUT_SECONDS = 5.0


async def _explicit_acl_manage(
    user_id: str,
    device_id: str,
    authorization: str,
    acl_service_url: str,
) -> bool:
    """Ask the ACL service whether the user has an explicit `manage` grant."""
    url = f"{acl_service_url.rstrip('/')}/check"
    body = {
        "user_id": user_id,
        "resource_type": "device",
        "resource_id": device_id,
        "permission": "manage",
    }
    headers = {"Authorization": authorization}
    try:
        async with httpx.AsyncClient(timeout=_ACL_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.info("acl_check_unreachable", extra={"error": str(exc)})
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("allowed", False))
    except ValueError:
        return False


async def _owns_active_reservation(
    user_id: str,
    device_id: str,
    reservations_service_url: str,
    internal_api_token: str,
) -> bool:
    """Ask reservations service whether the user owns an active reservation
    containing this device.

    Uses INTERNAL_API_TOKEN auth (not the caller's JWT) because reservations'
    /internal/active endpoint is the service-to-service surface. The caller
    is identified by the user_id query parameter, not by the bearer token.
    """
    if not internal_api_token:
        return False
    url = f"{reservations_service_url.rstrip('/')}/internal/active"
    headers = {"X-Internal-Token": internal_api_token}
    params = {"user_id": user_id, "device_id": device_id}
    try:
        async with httpx.AsyncClient(timeout=_ACL_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        logger.info("reservations_active_unreachable", extra={"error": str(exc)})
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("owns_active", False))
    except ValueError:
        return False


async def user_has_manage_or_owns_active_reservation(
    *,
    user_id: str,
    device_id: str,
    authorization: str | None,
    acl_service_url: str,
    reservations_service_url: str,
    internal_api_token: str,
) -> bool:
    """True iff the caller has explicit `manage` ACL on this device OR owns
    an ACTIVE reservation that includes this device.

    The reservation-owner free pass is a deliberate trust trade documented
    in docs/ROLES.md: a reservation owner is treated as effectively
    administrating their own reserved devices for the duration of the
    window. This matches the iter-2 precedent on GET /api/execution/runs.

    Closed-by-default on any error. The bearer token is required for the
    explicit-grant check (it threads through to the ACL service) but not
    for the reservation-owner fallback (that uses INTERNAL_API_TOKEN).
    """
    user_id_str = str(user_id)
    device_id_str = str(device_id)
    if authorization:
        if await _explicit_acl_manage(user_id_str, device_id_str, authorization, acl_service_url):
            return True
    return await _owns_active_reservation(
        user_id_str,
        device_id_str,
        reservations_service_url,
        internal_api_token,
    )
