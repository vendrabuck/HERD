"""Device-group visibility for execution's non-admin device-scoped reads.

Execution owns no device-group data, so a non-admin read that names devices
(the run list, a run's transcript, a device's health snapshot) asks inventory's
`GET /device-groups/visible-devices` with the caller's own bearer token, once per
request, and keeps only what that answer lists. This module is the one place
execution resolves that answer; a new non-admin device-scoped read must use it.

The lookup fails closed: a missing token, a transport error, a non-200, a body
that is not JSON, or a body that is not `{"device_ids": [<str>, ...]}` is not an
answer, and the route answers 503 `VISIBILITY_UNAVAILABLE_DETAIL` with no rows.
Admins are never filtered and never cause a lookup. The shape follows
reservations' `_fetch_visible_device_ids_strict`.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import HTTPException
from herd_common.auth import ADMIN_ROLES

from app.config import settings

logger = logging.getLogger(__name__)

# Pinned: tests match on this exact string.
VISIBILITY_UNAVAILABLE_DETAIL = (
    "Could not verify device visibility; nothing was returned. Retry the request."
)

_VISIBLE_DEVICES_TIMEOUT_SECONDS = 10.0


class VisibleDevicesUnavailable(Exception):
    """Inventory could not answer which devices the caller may see."""


async def fetch_visible_device_ids(user_id: str, authorization: str | None) -> set[uuid.UUID]:
    """Ask inventory which devices this user may see; raise when it cannot answer.

    Forwards the caller's Authorization header unchanged: the inventory route is
    JWT-guarded and answers only for the token's own subject. Every failure
    raises VisibleDevicesUnavailable; the reason goes to the log, never to the
    caller.
    """
    if not authorization:
        raise VisibleDevicesUnavailable("no authorization header")
    url = f"{settings.inventory_service_url.rstrip('/')}/device-groups/visible-devices"
    try:
        async with httpx.AsyncClient(timeout=_VISIBLE_DEVICES_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url,
                params={"user_id": str(user_id)},
                headers={"Authorization": authorization},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "visible-devices lookup failed for user %s (%s)", user_id, type(exc).__name__
        )
        raise VisibleDevicesUnavailable("transport error") from exc
    if resp.status_code != 200:
        logger.warning("visible-devices answered %s for user %s", resp.status_code, user_id)
        raise VisibleDevicesUnavailable(f"status {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("visible-devices answered a non-JSON body for user %s", user_id)
        raise VisibleDevicesUnavailable("unparseable body") from exc
    ids = data.get("device_ids") if isinstance(data, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        logger.warning("visible-devices answered a misshapen body for user %s", user_id)
        raise VisibleDevicesUnavailable("misshapen body")
    visible: set[uuid.UUID] = set()
    for raw in ids:
        try:
            visible.add(uuid.UUID(raw))
        except ValueError as exc:
            logger.warning("visible-devices answered a non-UUID id for user %s", user_id)
            raise VisibleDevicesUnavailable("misshapen body") from exc
    return visible


async def resolve_caller_visibility(
    payload: dict, authorization: str | None
) -> set[uuid.UUID] | None:
    """The devices a caller may see: None for an admin (no filter), else the set.

    Raises HTTPException 503 `VISIBILITY_UNAVAILABLE_DETAIL` when a non-admin's
    visibility cannot be determined, so a route never answers unfiltered.
    """
    if payload.get("role") in ADMIN_ROLES:
        return None
    try:
        return await fetch_visible_device_ids(str(payload.get("sub")), authorization)
    except VisibleDevicesUnavailable:
        raise HTTPException(status_code=503, detail=VISIBILITY_UNAVAILABLE_DETAIL) from None
