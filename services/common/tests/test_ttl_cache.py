"""Tests for herd_common.ttl_cache: TTLCache (per-key) and SingletonTTLCache.

Covers hit, miss, expiry via an injected clock, the concurrent-callers case
(N simultaneous misses on the same key must collapse into exactly one
upstream fetch, the whole point of the double-checked lock), and that the
singleton and per-key shapes are distinct and cannot be swapped for each
other.
"""

import asyncio

import pytest
from herd_common.ttl_cache import SingletonTTLCache, TTLCache


class _FakeClock:
    """Manually advanceable clock so expiry does not depend on real time."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


# --- TTLCache: hit / miss / expiry ---


@pytest.mark.asyncio
async def test_miss_then_fetches_and_stores():
    calls = []

    async def fetch(key: str) -> str:
        calls.append(key)
        return f"value-{key}"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    result = await cache.get("a")

    assert result == "value-a"
    assert calls == ["a"]


@pytest.mark.asyncio
async def test_hit_within_ttl_does_not_refetch():
    calls = []

    async def fetch(key: str) -> str:
        calls.append(key)
        return f"value-{key}"

    clock = _FakeClock()
    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60, clock=clock)

    await cache.get("a")
    clock.advance(10)
    result = await cache.get("a")

    assert result == "value-a"
    assert calls == ["a"]


@pytest.mark.asyncio
async def test_expiry_via_injected_clock_forces_refetch():
    calls = []

    async def fetch(key: str) -> str:
        calls.append(key)
        return f"value-{key}-{len(calls)}"

    clock = _FakeClock()
    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=10, clock=clock)

    first = await cache.get("a")
    clock.advance(11)  # past the 10s TTL
    second = await cache.get("a")

    assert calls == ["a", "a"]
    assert first != second


@pytest.mark.asyncio
async def test_different_keys_cached_independently():
    calls = []

    async def fetch(key: str) -> str:
        calls.append(key)
        return f"value-{key}"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    a = await cache.get("a")
    b = await cache.get("b")
    a_again = await cache.get("a")

    assert (a, b, a_again) == ("value-a", "value-b", "value-a")
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_invalidate_forces_refetch():
    calls = []

    async def fetch(key: str) -> str:
        calls.append(key)
        return f"value-{key}-{len(calls)}"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    first = await cache.get("a")
    cache.invalidate("a")
    second = await cache.get("a")

    assert calls == ["a", "a"]
    assert first != second


@pytest.mark.asyncio
async def test_concurrent_callers_collapse_into_one_fetch_per_key():
    """N simultaneous misses on the SAME key must trigger exactly one fetch.

    This is the behavior the double-checked lock exists for: the loser of
    the lock race must see the winner's freshly-stored value on its
    in-lock re-check, not issue its own redundant fetch.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch(key: str) -> str:
        calls["n"] += 1
        started.set()
        await release.wait()
        return f"value-{key}"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)

    async def caller():
        return await cache.get("shared")

    first = asyncio.create_task(caller())
    await started.wait()
    others = [asyncio.create_task(caller()) for _ in range(9)]
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(first, *others)

    assert calls["n"] == 1
    assert all(r == "value-shared" for r in results)


# --- SingletonTTLCache: hit / miss / expiry ---


@pytest.mark.asyncio
async def test_singleton_miss_then_fetches_and_stores():
    calls = {"n": 0}

    async def fetch() -> list[str]:
        calls["n"] += 1
        return ["x", "y"]

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    result = await cache.get()

    assert result == ["x", "y"]
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_singleton_hit_within_ttl_does_not_refetch():
    calls = {"n": 0}

    async def fetch() -> list[str]:
        calls["n"] += 1
        return ["x"]

    clock = _FakeClock()
    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(
        fetch=fetch, ttl_seconds=60, clock=clock
    )

    await cache.get()
    clock.advance(10)
    await cache.get()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_singleton_expiry_via_injected_clock_forces_refetch():
    calls = {"n": 0}

    async def fetch() -> int:
        calls["n"] += 1
        return calls["n"]

    clock = _FakeClock()
    cache: SingletonTTLCache[int] = SingletonTTLCache(fetch=fetch, ttl_seconds=10, clock=clock)

    first = await cache.get()
    clock.advance(11)
    second = await cache.get()

    assert calls["n"] == 2
    assert first != second


@pytest.mark.asyncio
async def test_singleton_invalidate_forces_refetch():
    calls = {"n": 0}

    async def fetch() -> int:
        calls["n"] += 1
        return calls["n"]

    cache: SingletonTTLCache[int] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    first = await cache.get()
    cache.invalidate()
    second = await cache.get()

    assert calls["n"] == 2
    assert first != second


@pytest.mark.asyncio
async def test_singleton_concurrent_callers_collapse_into_one_fetch():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch() -> list[str]:
        calls["n"] += 1
        started.set()
        await release.wait()
        return ["admin"]

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)

    async def caller():
        return await cache.get()

    first = asyncio.create_task(caller())
    await started.wait()
    others = [asyncio.create_task(caller()) for _ in range(9)]
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(first, *others)

    assert calls["n"] == 1
    assert all(r == ["admin"] for r in results)


# --- Singleton vs per-key shapes are distinct ---


def test_singleton_cache_has_no_key_parameter_on_get():
    """SingletonTTLCache.get takes no key; TTLCache.get requires one.

    Pins the shape distinction the issue calls out: a helper that collapsed
    both into one dict-keyed cache would regress AdminListClient, whose
    cached value is genuinely global, not keyed per anything.
    """
    import inspect

    singleton_params = list(inspect.signature(SingletonTTLCache.get).parameters)
    per_key_params = list(inspect.signature(TTLCache.get).parameters)

    assert singleton_params == ["self"]
    assert per_key_params == ["self", "key"]


def test_singleton_and_per_key_are_distinct_classes():
    assert not issubclass(SingletonTTLCache, TTLCache)
    assert not issubclass(TTLCache, SingletonTTLCache)
