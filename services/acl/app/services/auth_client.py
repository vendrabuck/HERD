import logging
import uuid

import httpx

logger = logging.getLogger(__name__)


async def fetch_user_groups(
    auth_service_url: str, user_id: uuid.UUID, token: str
) -> list[uuid.UUID]:
    """Fetch group IDs that a user belongs to from the auth service."""
    url = f"{auth_service_url}/groups/user/{user_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        if resp.status_code == 200:
            groups = resp.json()
            return [uuid.UUID(g["id"]) for g in groups]
        logger.warning(
            "Auth service returned %d for user groups lookup: user_id=%s",
            resp.status_code,
            user_id,
        )
        return []
    except httpx.HTTPError as exc:
        logger.error(
            "Failed to fetch user groups from auth service: %s",
            exc,
            extra={"user_id": str(user_id)},
        )
        return []


async def fetch_user_groups_internal(
    auth_service_url: str, user_id: uuid.UUID, internal_token: str
) -> list[uuid.UUID]:
    """Fetch group IDs for a user via auth's internal-token-guarded route.

    Sibling to fetch_user_groups above, for a caller with no user JWT to
    forward: POST /internal/check (issue #704) resolves group membership
    this way when a background job's authority is re-checked at fire time
    with only the job creator's user_id, not a bearer token.

    Closed-by-default: a missing token, a transport failure, or a non-200
    response from auth all return an empty list, which check_permission
    already treats as "no grant possible" rather than raising.
    """
    if not internal_token:
        return []
    url = f"{auth_service_url}/internal/users/{user_id}/groups"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"X-Internal-Token": internal_token},
                timeout=10.0,
            )
        if resp.status_code == 200:
            groups = resp.json()
            return [uuid.UUID(g["id"]) for g in groups]
        logger.warning(
            "Auth service returned %d for internal user groups lookup: user_id=%s",
            resp.status_code,
            user_id,
        )
        return []
    except httpx.HTTPError as exc:
        logger.error(
            "Failed to fetch user groups from auth service (internal): %s",
            exc,
            extra={"user_id": str(user_id)},
        )
        return []
