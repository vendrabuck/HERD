"""Async double-checked-locking TTL cache, shared by three notifications clients.

`ContactClient`, `PreferencesClient`, and `AdminListClient` (all in
`services/notifications/app/services/`) each hand-roll the identical
check-cache, take-lock, re-check-cache, fetch, store sequence around one
`asyncio.Lock`. The re-check after acquiring the lock is load-bearing: it is
what collapses N concurrent cache-misses on the same key into exactly one
upstream fetch, since the caller that loses the race to acquire the lock
finds the winner's result already stored when it gets in.

Two shapes, matching the two things being extracted:

- `TTLCache[K, V]`: per-key cache (`ContactClient`, `PreferencesClient`),
  keyed on an arbitrary hashable `K`.
- `SingletonTTLCache[V]`: one cached value with no key (`AdminListClient`,
  whose docstring says outright "the list is global, not per-user"). This is
  a distinct class, not `TTLCache` with an implicit key, so a caller cannot
  accidentally collapse a singleton cache into a dict-keyed one.

Both take a `fetch` callable (the caller's existing `_fetch` method/coroutine
function) and a `ttl_seconds`. The clock is `time.monotonic` by default,
matching every original, and is injectable for tests that need to force
expiry deterministically instead of sleeping.

Usage (per-key):

    from herd_common.ttl_cache import TTLCache

    cache: TTLCache[uuid.UUID, UserContact | None] = TTLCache(
        fetch=self._fetch, ttl_seconds=self._ttl,
    )
    contact = await cache.get(user_id)
    cache.invalidate(user_id)

Usage (singleton):

    from herd_common.ttl_cache import SingletonTTLCache

    cache: SingletonTTLCache[list[uuid.UUID]] = SingletonTTLCache(
        fetch=self._fetch, ttl_seconds=self._ttl,
    )
    admins = await cache.get()
    cache.invalidate()
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

Clock = Callable[[], float]


class TTLCache(Generic[K, V]):
    """Per-key async TTL cache with double-checked locking on one shared lock.

    One `asyncio.Lock` guards every key, matching the originals (each of
    `ContactClient`/`PreferencesClient` used a single lock for its whole
    cache, not a per-key lock), so a miss on key A still serializes behind a
    concurrent miss on key B. That is the existing behavior being preserved,
    not a new constraint being introduced.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[K], Awaitable[V]],
        ttl_seconds: float,
        clock: Clock = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[K, tuple[float, V]] = {}
        self._lock = asyncio.Lock()

    def _cache_hit(self, key: K) -> tuple[bool, V | None]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if expires_at < self._clock():
            return False, None
        return True, value

    async def get(self, key: K) -> V:
        hit, value = self._cache_hit(key)
        if hit:
            return value  # type: ignore[return-value]

        async with self._lock:
            hit, value = self._cache_hit(key)
            if hit:
                return value  # type: ignore[return-value]
            value = await self._fetch(key)
            self._cache[key] = (self._clock() + self._ttl, value)
            return value

    def invalidate(self, key: K) -> None:
        self._cache.pop(key, None)


class SingletonTTLCache(Generic[V]):
    """One cached value, no key. Mirrors `TTLCache` but for a global fetch.

    `AdminListClient`'s cache shape: `_cached_until: float` plus a bare
    value (there `list[uuid.UUID]`), not a dict. Kept as a distinct class
    (see module docstring) rather than `TTLCache` with a sentinel key.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], Awaitable[V]],
        ttl_seconds: float,
        clock: Clock = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached_until: float = 0.0
        self._cached: V | None = None
        self._lock = asyncio.Lock()

    def _cache_hit(self) -> tuple[bool, V | None]:
        if self._cached_until > self._clock():
            return True, self._cached
        return False, None

    async def get(self) -> V:
        hit, value = self._cache_hit()
        if hit:
            return value  # type: ignore[return-value]
        async with self._lock:
            hit, value = self._cache_hit()
            if hit:
                return value  # type: ignore[return-value]
            value = await self._fetch()
            self._cached = value
            self._cached_until = self._clock() + self._ttl
            return value

    def invalidate(self) -> None:
        self._cached_until = 0.0
        self._cached = None


__all__ = ["TTLCache", "SingletonTTLCache"]
