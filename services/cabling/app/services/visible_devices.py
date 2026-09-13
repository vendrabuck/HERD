"""Client for inventory's caller-visible-device-ids lookup (issue #719).

`GET /connections` used to return every connection in the fleet to any
authenticated user, which handed a non-admin caller the shape of gear they
cannot otherwise see (device ids and port names) as a reconnaissance step.
This module resolves the calling user's visible device ids from inventory so
the route can filter to connections touching at least one of them.

Issue #763 gave the same lookup two more consumers, both for the same reason
(a route that answered questions about device ids the caller cannot see):
the user-facing topology validate route, which reports a canvas node naming a
device outside the caller's visibility as ``missing_device`` rather than
running the edge and L3 passes against it, and the two pathfind routes, which
refuse a hidden endpoint and redact hidden transit hops.
``resolve_caller_visibility`` below is the shared route-side entry point for
all three (admin means no filter; an unanswerable lookup is a 503).

Distinct from device_group_guard.py's fetch_device_group_ids: that answers
"what groups is this one device in" (used to enforce the cross-group cabling
boundary at write time, fail-open on an unverifiable device), while this
answers "which devices can this user see" (used to filter a read at list
time, fail-closed on an unreachable inventory: see fetch_visible_device_ids's
docstring). The two must not be merged; their failure-mode contracts differ.
"""

import logging
import uuid

import httpx
from fastapi import HTTPException
from herd_common.auth import ADMIN_ROLES
from herd_common.internal_client import ForwardedAuth, call_service

from app.config import settings

logger = logging.getLogger(__name__)


class VisibleDevicesUnavailableError(Exception):
    """Raised when inventory's visible-devices lookup could not be answered.

    Covers both a transport failure and a non-2xx response; the caller
    (list_connections_endpoint) maps this to a 503 and returns nothing,
    since a non-admin's device visibility is a security boundary and must
    fail CLOSED, not fall back to an unfiltered fleet-wide list.
    """


async def fetch_visible_device_ids(caller_id: uuid.UUID, authorization: str) -> set[uuid.UUID]:
    """Return the set of device ids visible to the calling (non-admin) user.

    Forwards the caller's own JWT to inventory's self-service
    `GET /device-groups/visible-devices?user_id=<caller_id>` (the same route
    the topology editor and other visibility checks resolve through); that
    route 403s a request for any user_id other than the caller's own, so
    caller_id must be the caller's own `sub`.

    Raises VisibleDevicesUnavailableError on any transport error or non-2xx
    response so the caller fails closed. Never returns None: an empty set is
    a genuine "sees nothing", not "could not verify".
    """
    try:
        resp = await call_service(
            settings.inventory_service_url,
            "GET",
            "/device-groups/visible-devices",
            params={"user_id": str(caller_id)},
            timeout=5.0,
            auth=ForwardedAuth(authorization=authorization),
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "visible_devices_unreachable",
            extra={"caller_id": str(caller_id), "error": str(exc)},
        )
        raise VisibleDevicesUnavailableError(str(exc)) from exc
    if resp.status_code != 200:
        logger.warning(
            "visible_devices_bad_response",
            extra={"caller_id": str(caller_id), "status": resp.status_code},
        )
        raise VisibleDevicesUnavailableError(f"inventory returned {resp.status_code}")
    body = resp.json()
    return {uuid.UUID(d) for d in body.get("device_ids", [])}


async def resolve_caller_visibility(
    payload: dict,
    authorization: str | None,
    *,
    unavailable_detail: str,
) -> set[uuid.UUID] | None:
    """Resolve the visibility filter that applies to this caller, or None.

    Returns None for an admin or superadmin caller, meaning "no filter
    applies": admins see the whole fleet and never trigger the inventory
    lookup. For every other role it returns the caller's visible device id
    set (possibly empty, a genuine "sees nothing").

    Raises HTTPException 503 with ``unavailable_detail`` when the lookup
    could not be answered, so each route keeps its own wording while the
    fail-closed rule itself lives in one place: a non-admin's device
    visibility is a security boundary and an unverifiable answer must never
    degrade into an unfiltered one (issue #763, the same rule
    ``list_connections_endpoint`` applies for issue #719).
    """
    if payload.get("role") in ADMIN_ROLES:
        return None
    if authorization is None:
        raise HTTPException(
            status_code=500,
            detail="internal: missing Authorization header while resolving device visibility",
        )
    try:
        return await fetch_visible_device_ids(uuid.UUID(payload["sub"]), authorization)
    except VisibleDevicesUnavailableError as exc:
        logger.warning(
            "caller_visibility_unavailable",
            extra={"caller_id": payload.get("sub"), "error": str(exc)},
        )
        raise HTTPException(status_code=503, detail=unavailable_detail) from exc
