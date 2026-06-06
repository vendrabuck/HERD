import asyncio
import logging
import time
import uuid

import httpx

from app.config import settings
from app.schemas.preferences import NotificationPreferences

logger = logging.getLogger(__name__)


class PreferencesClient:
    """Reads `extras.notifications` from user-profile via the internal endpoint.

    Cached in-process with a short TTL to avoid hammering user-profile from the
    consumer loop. TTL is bounded by `preferences_cache_ttl_seconds`.
    """

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        ttl_seconds: int | None = None,
    ):
        self._base_url = (base_url or settings.user_profile_service_url).rstrip("/")
        self._token = internal_token if internal_token is not None else settings.internal_api_token
        self._ttl = (
            ttl_seconds if ttl_seconds is not None else settings.preferences_cache_ttl_seconds
        )
        self._cache: dict[uuid.UUID, tuple[float, NotificationPreferences]] = {}
        self._lock = asyncio.Lock()

    def _cache_hit(self, user_id: uuid.UUID) -> NotificationPreferences | None:
        entry = self._cache.get(user_id)
        if entry is None:
            return None
        expires_at, prefs = entry
        if expires_at < time.monotonic():
            return None
        return prefs

    async def get(self, user_id: uuid.UUID) -> NotificationPreferences:
        cached = self._cache_hit(user_id)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._cache_hit(user_id)
            if cached is not None:
                return cached

            prefs = await self._fetch(user_id)
            self._cache[user_id] = (time.monotonic() + self._ttl, prefs)
            return prefs

    async def _fetch(self, user_id: uuid.UUID) -> NotificationPreferences:
        url = f"{self._base_url}/preferences/internal"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"user_id": str(user_id)},
                    headers={"X-Internal-Token": self._token},
                    timeout=5.0,
                )
        except httpx.HTTPError:
            logger.warning(
                "Failed to fetch preferences; falling back to defaults",
                extra={"action": "prefs_fetch_failed", "user_id": str(user_id)},
                exc_info=True,
            )
            return NotificationPreferences.with_defaults(None)

        if resp.status_code != 200:
            logger.warning(
                "Non-200 from user-profile /preferences/internal; falling back to defaults",
                extra={
                    "action": "prefs_fetch_non_200",
                    "user_id": str(user_id),
                    "status": resp.status_code,
                },
            )
            return NotificationPreferences.with_defaults(None)

        payload = resp.json()
        stored = (payload.get("extras") or {}).get("notifications")
        return NotificationPreferences.with_defaults(stored)

    def invalidate(self, user_id: uuid.UUID) -> None:
        self._cache.pop(user_id, None)


_client: PreferencesClient | None = None


def get_preferences_client() -> PreferencesClient:
    global _client
    if _client is None:
        _client = PreferencesClient()
    return _client


def set_preferences_client(client: PreferencesClient | None) -> None:
    global _client
    _client = client
