"""Recipient resolver for device.health_transition events.

The notifications service fans out a health-transition notification to
admins (cached) + users with an active reservation on the affected
device (not cached: cheap per-device lookup, churns frequently).

Failures on either side log a warning and return what was successfully
fetched. If both fetches fail the resolver returns an empty list and the
dispatch silently produces zero messages; matches the closed-default
posture used elsewhere when an upstream service is unreachable.
"""

import asyncio
import logging
import time
import uuid

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class AdminListClient:
    """TTL-cached fetcher for the list of admin user-ids.

    Admin list rarely changes so a single in-process cache is fine. TTL
    is bounded by `settings.health_notify_admin_cache_ttl_seconds`.
    Mirror of `preferences_client.PreferencesClient` cache shape, but
    keyed on a singleton (the list is global, not per-user).
    """

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        ttl_seconds: int | None = None,
    ):
        self._base_url = (base_url or settings.auth_service_url).rstrip("/")
        self._token = internal_token if internal_token is not None else settings.internal_api_token
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else settings.health_notify_admin_cache_ttl_seconds
        )
        self._cached_until: float = 0.0
        self._cached: list[uuid.UUID] = []
        self._lock = asyncio.Lock()

    async def list_admins(self) -> list[uuid.UUID]:
        if self._cached_until > time.monotonic():
            return list(self._cached)
        async with self._lock:
            if self._cached_until > time.monotonic():
                return list(self._cached)
            admins = await self._fetch()
            self._cached = admins
            self._cached_until = time.monotonic() + self._ttl
            return list(admins)

    async def _fetch(self) -> list[uuid.UUID]:
        if not self._token:
            logger.warning(
                "health_recipients: INTERNAL_API_TOKEN unset; cannot fetch admin list",
                extra={"action": "admin_fetch_no_token"},
            )
            return []
        url = f"{self._base_url}/internal/admins"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers={"X-Internal-Token": self._token}, timeout=5.0)
        except httpx.HTTPError:
            logger.warning(
                "Failed to fetch admin list from auth; returning empty",
                extra={"action": "admin_fetch_failed"},
                exc_info=True,
            )
            return []
        if resp.status_code != 200:
            logger.warning(
                "Non-200 from auth /internal/admins; returning empty",
                extra={"action": "admin_fetch_non_200", "status": resp.status_code},
            )
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.warning("auth /internal/admins returned malformed JSON")
            return []
        out: list[uuid.UUID] = []
        for item in data:
            try:
                out.append(uuid.UUID(str(item)))
            except (ValueError, TypeError):
                continue
        return out

    def invalidate(self) -> None:
        self._cached_until = 0.0
        self._cached = []


async def _fetch_active_reservation_holders(device_id: uuid.UUID) -> list[uuid.UUID]:
    """Return user_ids of every active reservation on this device.

    Per-device lookup: not cached, but cheap (single ACTIVE-status DB
    scan filtered to the device).
    """
    if not settings.internal_api_token:
        logger.warning(
            "health_recipients: INTERNAL_API_TOKEN unset; cannot fetch reservation holders",
            extra={"action": "holders_fetch_no_token"},
        )
        return []
    url = f"{settings.reservations_service_url.rstrip('/')}/internal/active-users"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"device_id": str(device_id)},
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=5.0,
            )
    except httpx.HTTPError:
        logger.warning(
            "Failed to fetch reservation holders from reservations; returning empty",
            extra={"action": "holders_fetch_failed", "device_id": str(device_id)},
            exc_info=True,
        )
        return []
    if resp.status_code != 200:
        logger.warning(
            "Non-200 from reservations /internal/active-users; returning empty",
            extra={
                "action": "holders_fetch_non_200",
                "device_id": str(device_id),
                "status": resp.status_code,
            },
        )
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("reservations /internal/active-users returned malformed JSON")
        return []
    out: list[uuid.UUID] = []
    for item in data:
        try:
            out.append(uuid.UUID(str(item)))
        except (ValueError, TypeError):
            continue
    return out


_admin_client: AdminListClient | None = None


def get_admin_client() -> AdminListClient:
    global _admin_client
    if _admin_client is None:
        _admin_client = AdminListClient()
    return _admin_client


def set_admin_client(client: AdminListClient | None) -> None:
    """Test seam: swap the module-level singleton (e.g. for fakes)."""
    global _admin_client
    _admin_client = client


async def resolve_health_recipients(device_id: uuid.UUID) -> list[uuid.UUID]:
    """Deduped union of admin user-ids and active reservation holders."""
    admins = await get_admin_client().list_admins()
    holders = await _fetch_active_reservation_holders(device_id)
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for uid in (*admins, *holders):
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out
