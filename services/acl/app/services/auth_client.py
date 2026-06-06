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
