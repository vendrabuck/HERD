"""Client for inventory's caller-visible-device-ids lookup (issue #719).

`GET /connections` used to return every connection in the fleet to any
authenticated user, which handed a non-admin caller the shape of gear they
cannot otherwise see (device ids and port names) as a reconnaissance step.
This module resolves the calling user's visible device ids from inventory so
the route can filter to connections touching at least one of them.

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
