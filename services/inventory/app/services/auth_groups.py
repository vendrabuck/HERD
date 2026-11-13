"""Inventory's reads of the auth service's user groups, forwarding the caller's JWT.

The single home of the two lookups (issue #1339). They lived in
``app/routers/device_groups.py`` as private helpers, and ``device_visibility``, the
service-layer home of inventory's one non-admin visibility path, imported them from
that router inside its function bodies to dodge the import cycle the router's own
import of ``device_visibility`` created. Both the router and ``device_visibility``
now import this module, and call through it (``auth_groups.fetch_user_group_ids``),
so one patch of this module's attribute reaches every caller.

Both lookups fail closed: an unreachable auth service or a non-200 answer is a 503,
never an empty result, and a missing Authorization header is a 500 (the bearer
dependency on every route guarantees one).
"""

import logging
import uuid

import httpx
from fastapi import HTTPException
from herd_common.internal_client import ForwardedAuth, call_service

from app.config import settings

logger = logging.getLogger(__name__)

# Auth's GET /groups caps `limit` at 500 (services/auth/app/routers/groups.py).
AUTH_GROUP_PAGE_SIZE = 500


def _auth_base() -> str:
    return settings.auth_service_url.rstrip("/")


async def fetch_user_group_ids(user_id: uuid.UUID, authorization: str | None) -> list[uuid.UUID]:
    """Fetch user's group IDs from the auth service, forwarding the caller JWT.

    Raises HTTPException(503) on upstream failure. Callers that want fail-open
    behavior (e.g. show-all-devices on auth outage) must wrap this call in
    their own try/except.

    A None authorization is a contract violation (the bearer dep on each route
    guarantees it's present) and raises HTTPException(500).
    """
    if authorization is None:
        raise HTTPException(
            status_code=500,
            detail="internal: missing Authorization header while resolving user groups",
        )
    try:
        resp = await call_service(
            _auth_base(),
            "GET",
            f"/groups/user/{user_id}",
            timeout=10.0,
            auth=ForwardedAuth(authorization=authorization),
        )
    except httpx.HTTPError as exc:
        logger.error("auth service unreachable while fetching groups for user %s: %s", user_id, exc)
        raise HTTPException(
            status_code=503, detail="auth service unreachable while fetching user groups"
        ) from exc

    if resp.status_code != 200:
        logger.error(
            "auth service returned %s for user %s groups: %s",
            resp.status_code,
            user_id,
            resp.text[:200],
        )
        raise HTTPException(
            status_code=503,
            detail=f"auth service returned {resp.status_code} when fetching user groups",
        )

    groups = resp.json()
    return [uuid.UUID(g["id"]) for g in groups]


async def fetch_user_group_names(
    user_group_ids: list[uuid.UUID],
    authorization: str | None,
) -> dict[uuid.UUID, str]:
    """Fetch user group names from the auth service. Returns a map of id to name.

    Raises HTTPException(503) on upstream failure. A None authorization raises
    HTTPException(500); the bearer dep on each route guarantees it's present.
    """
    if not user_group_ids:
        return {}
    if authorization is None:
        raise HTTPException(
            status_code=500,
            detail="internal: missing Authorization header while resolving group names",
        )
    # Page through auth's group list until every wanted id is named or the list
    # ends (issue #1021). Reading one default page (50, oldest first) left every
    # later group with a null name. Auth has no by-ids lookup open to a non-admin
    # caller, so paging at auth's maximum page size is the narrowest read.
    wanted = set(user_group_ids)
    name_map: dict[uuid.UUID, str] = {}
    skip = 0
    while True:
        try:
            resp = await call_service(
                _auth_base(),
                "GET",
                "/groups",
                timeout=10.0,
                auth=ForwardedAuth(authorization=authorization),
                params={"skip": skip, "limit": AUTH_GROUP_PAGE_SIZE},
            )
        except httpx.HTTPError as exc:
            logger.error("auth service unreachable while fetching group names: %s", exc)
            raise HTTPException(
                status_code=503, detail="auth service unreachable while fetching group names"
            ) from exc

        if resp.status_code != 200:
            logger.error(
                "auth service returned %s for group names: %s",
                resp.status_code,
                resp.text[:200],
            )
            raise HTTPException(
                status_code=503,
                detail=f"auth service returned {resp.status_code} when fetching group names",
            )

        data = resp.json()
        groups = data.get("items", data) if isinstance(data, dict) else data
        for g in groups:
            gid = uuid.UUID(g["id"])
            if gid in wanted:
                name_map[gid] = g["name"]

        skip += len(groups)
        total = data.get("total") if isinstance(data, dict) else None
        if (
            len(name_map) == len(wanted)
            or not isinstance(data, dict)
            or len(groups) < AUTH_GROUP_PAGE_SIZE
            or (isinstance(total, int) and skip >= total)
        ):
            return name_map
