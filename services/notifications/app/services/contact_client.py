"""Resolve a user's contact info (email, username) for outbound channels.

The email and chat dispatchers need to address a message to a human. The auth
service owns user records, so this client calls its internal-token endpoint
GET /internal/users/{id}/contact. Results are cached in-process with a short
TTL to avoid hammering auth from the consumer loop, mirroring the cache shape
of `PreferencesClient`.

A lookup failure (auth unreachable, non-200, missing user) returns None; the
calling dispatcher logs and skips its channel without blocking the others.
"""

import logging
import uuid
from dataclasses import dataclass

import httpx
from herd_common.internal_client import InternalTokenAuth, call_service
from herd_common.ttl_cache import TTLCache

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserContact:
    user_id: uuid.UUID
    email: str
    username: str


class ContactClient:
    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        ttl_seconds: int | None = None,
    ):
        self._base_url = (base_url or settings.auth_service_url).rstrip("/")
        self._token = internal_token if internal_token is not None else settings.internal_api_token
        self._ttl = (
            ttl_seconds if ttl_seconds is not None else settings.preferences_cache_ttl_seconds
        )
        self._cache: TTLCache[uuid.UUID, UserContact | None] = TTLCache(
            fetch=self._fetch, ttl_seconds=self._ttl
        )

    async def get(self, user_id: uuid.UUID) -> UserContact | None:
        return await self._cache.get(user_id)

    async def _fetch(self, user_id: uuid.UUID) -> UserContact | None:
        if not self._token:
            logger.warning(
                "contact_client: INTERNAL_API_TOKEN unset; cannot resolve user contact",
                extra={"action": "contact_fetch_no_token", "user_id": str(user_id)},
            )
            return None
        try:
            resp = await call_service(
                self._base_url,
                "GET",
                f"/internal/users/{user_id}/contact",
                timeout=5.0,
                auth=InternalTokenAuth(token=self._token),
            )
        except httpx.HTTPError:
            logger.warning(
                "Failed to fetch user contact from auth; skipping outbound channel",
                extra={"action": "contact_fetch_failed", "user_id": str(user_id)},
                exc_info=True,
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "Non-200 from auth /internal/users/{id}/contact; skipping outbound channel",
                extra={
                    "action": "contact_fetch_non_200",
                    "user_id": str(user_id),
                    "status": resp.status_code,
                },
            )
            return None
        try:
            data = resp.json()
            return UserContact(
                user_id=user_id,
                email=str(data["email"]),
                username=str(data["username"]),
            )
        except (ValueError, KeyError, TypeError):
            logger.warning(
                "auth /internal/users/{id}/contact returned malformed JSON",
                extra={"action": "contact_fetch_malformed", "user_id": str(user_id)},
            )
            return None

    def invalidate(self, user_id: uuid.UUID) -> None:
        self._cache.invalidate(user_id)


_client: ContactClient | None = None


def get_contact_client() -> ContactClient:
    global _client
    if _client is None:
        _client = ContactClient()
    return _client


def set_contact_client(client: ContactClient | None) -> None:
    """Test seam: swap the module-level singleton."""
    global _client
    _client = client
