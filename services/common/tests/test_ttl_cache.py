"""Tests for herd_common.ttl_cache: TTLCache (per-key) and SingletonTTLCache.

Covers hit, miss, expiry via an injected clock, the concurrent-callers case
(N simultaneous misses on the same key must collapse into exactly one
upstream fetch, the whole point of the double-checked lock), and that the
singleton and per-key shapes are distinct and cannot be swapped for each
other.
"""

import asyncio

import pytest
from herd_common.ttl_cache import SingletonTTLCache, TTLCache, Uncached


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


# --- Uncached: a failure is not an answer (issue #1075) ---


@pytest.mark.asyncio
async def test_uncached_fallback_is_returned_but_not_stored():
    """A fetch that could not ask returns Uncached(fallback): this caller gets
    the bare fallback and the next call asks again, then caches the answer."""
    calls = []

    async def fetch(key: str):
        calls.append(key)
        if len(calls) == 1:
            return Uncached("fallback")
        return f"answer-{key}"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)

    first = await cache.get("a")
    second = await cache.get("a")
    third = await cache.get("a")

    assert first == "fallback"
    assert not isinstance(first, Uncached)
    assert (second, third) == ("answer-a", "answer-a")
    assert calls == ["a", "a"]


@pytest.mark.asyncio
async def test_uncached_failure_leaves_an_existing_entry_for_other_keys():
    async def fetch(key: str):
        return Uncached(None) if key == "down" else f"answer-{key}"

    calls = []

    async def counting_fetch(key: str):
        calls.append(key)
        return await fetch(key)

    cache: TTLCache[str, str | None] = TTLCache(fetch=counting_fetch, ttl_seconds=60)
    assert await cache.get("up") == "answer-up"
    assert await cache.get("down") is None
    assert await cache.get("down") is None
    assert await cache.get("up") == "answer-up"
    assert calls == ["up", "down", "down"]


@pytest.mark.asyncio
async def test_singleton_uncached_fallback_is_returned_but_not_stored():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            return Uncached([])
        return ["admin"]

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)

    first = await cache.get()
    second = await cache.get()
    third = await cache.get()

    assert first == []
    assert (second, third) == (["admin"], ["admin"])
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_uncached_falsy_answer_is_still_cached_when_not_wrapped():
    """An empty answer the upstream GAVE is cached; only the wrapper skips."""
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return []

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    await cache.get()
    await cache.get()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_failed_fetch_is_shared_with_the_callers_queued_behind_it():
    """A failure is an answer for the callers already waiting on it.

    The lock used to hand the failed fetch back to the caller that made it only,
    so each waiter then ran its own fetch: during an outage N callers made N
    sequential upstream attempts and the k-th waited for k timeouts. The
    in-flight fetch is now shared, `Uncached` included, while nothing is stored
    for later callers."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch(key: str):
        calls["n"] += 1
        started.set()
        await release.wait()
        return Uncached("fallback")

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    first = asyncio.create_task(cache.get("k"))
    await started.wait()
    queued = [asyncio.create_task(cache.get("k")) for _ in range(4)]
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, *queued) == ["fallback"] * 5
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_call_arriving_after_a_shared_failure_fetches_again():
    """Sharing the failure does not cache it: ADR 0016 D8 and issue #1075 hold."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch(key: str):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release.wait()
            return Uncached("fallback")
        return "answer"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    first = asyncio.create_task(cache.get("k"))
    await started.wait()
    second = asyncio.create_task(cache.get("k"))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == ["fallback", "fallback"]

    assert await cache.get("k") == "answer"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_singleton_a_failed_fetch_is_shared_with_waiters():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        started.set()
        await release.wait()
        return Uncached([])

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    first = asyncio.create_task(cache.get())
    await started.wait()
    queued = [asyncio.create_task(cache.get()) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, *queued) == [[], [], [], []]
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_failure_on_one_key_does_not_charge_another_key():
    """Per-key futures: one key's outage is not another key's fetch."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fetch(key: str):
        calls.append(key)
        if key == "down":
            started.set()
            await release.wait()
            return Uncached(None)
        return f"answer-{key}"

    cache: TTLCache[str, str | None] = TTLCache(fetch=fetch, ttl_seconds=60)
    down = asyncio.create_task(cache.get("down"))
    await started.wait()
    up = asyncio.create_task(cache.get("up"))

    # Assert the overlap the name claims: "up" finishes while "down" is still
    # blocked, without a wall-clock wait. Ten loop turns let "up" run to
    # completion while "down" is still parked on its event.
    for _ in range(10):
        await asyncio.sleep(0)
    assert up.done(), "'up' must have finished"
    assert up.result() == "answer-up"
    assert not down.done(), "'down' must still be waiting on its own fetch"
    release.set()

    assert await down is None
    assert calls == ["down", "up"]


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_cancel_the_shared_fetch():
    """A caller that goes away must not take the fetch down with it.

    Awaiting a bare future propagates the caller's cancellation into that
    future, so a waiter that times out would cancel the fetch for everyone.
    Every caller awaits the task through `asyncio.shield` instead, so a
    cancelled caller is answered with `CancelledError` while the others get
    the fetch's value.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fetch(key: str):
        calls["n"] += 1
        started.set()
        await release.wait()
        return "value"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    leader = asyncio.create_task(cache.get("k"))
    await started.wait()
    waiter = asyncio.create_task(cache.get("k"))
    bystander = asyncio.create_task(cache.get("k"))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    assert await asyncio.gather(leader, bystander) == ["value", "value"]
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_cancelling_the_leader_still_answers_the_waiters():
    """The first caller is a waiter like any other, so losing it costs nothing."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(key: str):
        started.set()
        await release.wait()
        return "value"

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    leader = asyncio.create_task(cache.get("k"))
    await started.wait()
    waiter = asyncio.create_task(cache.get("k"))
    await asyncio.sleep(0)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    release.set()
    assert await waiter == "value"


@pytest.mark.asyncio
async def test_an_exception_in_the_fetch_reaches_every_waiter_and_none_hangs():
    """The failure is shared as an exception too, and nobody is left waiting."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(key: str):
        started.set()
        await release.wait()
        raise RuntimeError("upstream is down")

    cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
    callers = [asyncio.create_task(cache.get("k")) for _ in range(3)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(*callers, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_the_singleton_shares_a_fetch_without_propagating_cancellation():
    """SingletonTTLCache has the same shape and the same property."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        return []

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    leader = asyncio.create_task(cache.get())
    await started.wait()
    waiter = asyncio.create_task(cache.get())
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    assert await leader == []


@pytest.mark.asyncio
async def test_the_singleton_answers_a_waiter_when_its_leader_is_cancelled():
    """The singleton's first caller is a waiter like any other."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        return []

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    leader = asyncio.create_task(cache.get())
    await started.wait()
    waiter = asyncio.create_task(cache.get())
    await asyncio.sleep(0)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    release.set()
    assert await waiter == []


@pytest.mark.asyncio
async def test_an_exception_in_the_singleton_fetch_reaches_every_waiter():
    """The singleton shares a raising fetch instead of hanging its callers."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        raise RuntimeError("upstream is down")

    cache: SingletonTTLCache[list[str]] = SingletonTTLCache(fetch=fetch, ttl_seconds=60)
    callers = [asyncio.create_task(cache.get()) for _ in range(3)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(*callers, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_a_failed_fetch_no_one_awaited_is_not_logged_as_unretrieved():
    """`_discard` reads a failed task after its last caller is cancelled.

    On Python 3.12 an unread task exception surfaces as "Task exception was
    never retrieved". On 3.14 `asyncio.shield` also logs "exception in
    shielded future" in this same case, whatever `_discard` does, so only the
    never-retrieved wording is pinned.
    """
    loop = asyncio.get_running_loop()
    logged: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: logged.append(str(context.get("message", "")))
    )
    try:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(key: str):
            started.set()
            await release.wait()
            raise RuntimeError("upstream is down")

        cache: TTLCache[str, str] = TTLCache(fetch=fetch, ttl_seconds=60)
        caller = asyncio.create_task(cache.get("k"))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert not [message for message in logged if "never retrieved" in message]
