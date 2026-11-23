"""Async TTL cache that shares an in-flight fetch, shared by three notifications clients.

`ContactClient`, `PreferencesClient`, and `AdminListClient` (all in
`services/notifications/app/services/`) each hand-roll the identical
check-cache, register-fetch, await-it sequence. Registering the fetch before
the caller awaits collapses N concurrent cache-misses on the same key into
exactly one upstream call, a failure included: the callers that arrive during
it are handed the same outcome instead of each running their own fetch.

The fetch runs as its own task rather than inside the caller that started it,
for two reasons. One caller going away must not cancel everyone else's answer:
awaiting a bare future propagates the caller's cancellation into it, so a
waiter that timed out would cancel the fetch for all of them; every caller
awaits the task through `asyncio.shield`. And fetches for different keys run
concurrently instead of one at a time per cache, which is safe because the
keys are independent upstream lookups.

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

A cancelled caller sees `CancelledError`; it does not cancel the fetch, and a
fetch that raises hands the exception to every waiter. Nothing is stored for a
failure, so a caller arriving after one still asks again.

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

A failure is not an answer (issue #1075). When `fetch` could not ask the
upstream (transport error, unexpected status, unreadable body, no token) it
returns its fallback wrapped in `Uncached(value)`: `get` hands every caller
queued on the fetch the bare value and stores nothing, so the next call asks
again instead of
replaying the outage for the whole TTL. Only the caller can tell a failure
from an answer (an empty admin list or default preferences can be either),
which is why the marking happens in `fetch` and the not-storing happens here.

    async def _fetch(self, key):
        try:
            resp = await call_service(...)
        except httpx.HTTPError:
            return Uncached(FALLBACK)
        return parse(resp)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

Clock = Callable[[], float]


@dataclass(frozen=True)
class Uncached(Generic[V]):
    """A fallback `fetch` returns when it could not get an answer.

    The cache returns `value` to every caller queued on that fetch and stores
    nothing, so the failure does not outlive the call that hit it.
    """

    value: V


def _discard(task: "asyncio.Task") -> None:
    """Read a finished task's exception so asyncio does not log it as unretrieved.

    A fetch that raises while no caller is waiting would otherwise surface as
    "Future exception was never retrieved". A cancelled task has no exception to
    read, and reading one would raise CancelledError here.
    """
    if not task.cancelled():
        task.exception()


class TTLCache(Generic[K, V]):
    """Per-key async TTL cache that shares an in-flight fetch between callers.

    The fetch itself runs as its own task, which is what lets one caller go
    away without taking everyone's answer with it: awaiting a bare future
    propagates the caller's cancellation into that future. `shield` keeps a
    cancelled caller from touching the task, and the task clears its own slot,
    so no caller can join a fetch that has already finished.

    Fetches for different keys run concurrently. Stated as a decision, not an
    accident: the keys are independent upstream lookups and nothing orders
    them.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[K], Awaitable[V | Uncached[V]]],
        ttl_seconds: float,
        clock: Clock = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[K, tuple[float, V]] = {}
        self._inflight: dict[K, asyncio.Task] = {}

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

        # The in-flight fetch is shared, a failure included: the callers already
        # queued on the key get the answer the first one paid for instead of each
        # running their own fetch, which during an outage meant N sequential
        # upstream attempts and the k-th caller waiting for k timeouts. Nothing
        # is stored for a failure, so a caller arriving after it still asks.
        # There is no await between the lookup above and the registration below,
        # so exactly one caller creates the task.
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._fetch_and_store(key))
            task.add_done_callback(_discard)
            if not task.done():
                self._inflight[key] = task

        # Every caller waits the same way, the first included: the fetch is not
        # bound to anyone's cancellation.
        return await asyncio.shield(task)

    async def _fetch_and_store(self, key: K) -> V:
        """One fetch, published, with the slot cleared even when it raises."""
        try:
            fetched = await self._fetch(key)
            if isinstance(fetched, Uncached):
                return fetched.value
            self._cache[key] = (self._clock() + self._ttl, fetched)
            return fetched
        finally:
            self._inflight.pop(key, None)

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
        fetch: Callable[[], Awaitable[V | Uncached[V]]],
        ttl_seconds: float,
        clock: Clock = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached_until: float = 0.0
        self._cached: V | None = None
        self._inflight: asyncio.Task | None = None

    def _cache_hit(self) -> tuple[bool, V | None]:
        if self._cached_until > self._clock():
            return True, self._cached
        return False, None

    async def get(self) -> V:
        hit, value = self._cache_hit()
        if hit:
            return value  # type: ignore[return-value]

        # Same shared in-flight fetch as TTLCache, and for the same reasons: one
        # fetch answers everyone waiting on it, and no caller's cancellation
        # reaches it. There is no await between the lookup above and the
        # registration below, so exactly one caller creates the task.
        task = self._inflight
        if task is None:
            task = asyncio.get_running_loop().create_task(self._fetch_and_store())
            task.add_done_callback(_discard)
            if not task.done():
                self._inflight = task

        return await asyncio.shield(task)

    async def _fetch_and_store(self) -> V:
        """One fetch, published, with the slot cleared even when it raises."""
        try:
            fetched = await self._fetch()
            if isinstance(fetched, Uncached):
                return fetched.value
            self._cached = fetched
            self._cached_until = self._clock() + self._ttl
            return fetched
        finally:
            self._inflight = None

    def invalidate(self) -> None:
        self._cached_until = 0.0
        self._cached = None


__all__ = ["TTLCache", "SingletonTTLCache", "Uncached"]
