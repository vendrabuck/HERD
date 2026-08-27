import logging
import uuid

import httpx
from herd_common.internal_client import InternalTokenAuth, call_service
from herd_common.ttl_cache import TTLCache

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
        self._cache: TTLCache[uuid.UUID, NotificationPreferences] = TTLCache(
            fetch=self._fetch, ttl_seconds=self._ttl
        )

    async def get(self, user_id: uuid.UUID) -> NotificationPreferences:
        return await self._cache.get(user_id)

    async def _fetch(self, user_id: uuid.UUID) -> NotificationPreferences:
        try:
            resp = await call_service(
                self._base_url,
                "GET",
                "/preferences/internal",
                params={"user_id": str(user_id)},
                timeout=5.0,
                auth=InternalTokenAuth(token=self._token),
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
        self._cache.invalidate(user_id)


_client: PreferencesClient | None = None


def get_preferences_client() -> PreferencesClient:
    global _client
    if _client is None:
        _client = PreferencesClient()
    return _client


def set_preferences_client(client: PreferencesClient | None) -> None:
    global _client
    _client = client
