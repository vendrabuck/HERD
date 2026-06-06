"""Tests for herd_common.retry."""

import pytest
from herd_common.retry import retry_with_backoff


class _Transient(Exception):
    pass


class _Fatal(Exception):
    pass


@pytest.mark.asyncio
async def test_returns_first_result_without_sleeping(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("herd_common.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await retry_with_backoff(fn, attempts=3, initial_delay=0.1)

    assert result == "ok"
    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_retries_until_success(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("herd_common.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _Transient(f"attempt {calls}")
        return "ok"

    result = await retry_with_backoff(fn, attempts=5, initial_delay=0.1, factor=2.0)

    assert result == "ok"
    assert calls == 3
    assert sleeps == [0.1, 0.2]


@pytest.mark.asyncio
async def test_exhausts_and_raises_last_exception(monkeypatch):
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("herd_common.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise _Transient(f"boom {calls}")

    with pytest.raises(_Transient, match="boom 3"):
        await retry_with_backoff(fn, attempts=3, initial_delay=0.01)

    assert calls == 3


@pytest.mark.asyncio
async def test_non_retryable_exception_bypasses_retry(monkeypatch):
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("herd_common.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise _Fatal("do not retry")

    with pytest.raises(_Fatal):
        await retry_with_backoff(fn, attempts=5, initial_delay=0.01, retryable=(_Transient,))

    assert calls == 1


@pytest.mark.asyncio
async def test_on_retry_is_called_for_each_failure(monkeypatch):
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("herd_common.retry.asyncio.sleep", fake_sleep)

    observed: list[tuple[int, str]] = []

    def on_retry(attempt: int, exc: BaseException) -> None:
        observed.append((attempt, str(exc)))

    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise _Transient(f"try {calls}")

    with pytest.raises(_Transient):
        await retry_with_backoff(fn, attempts=3, initial_delay=0.01, on_retry=on_retry)

    assert observed == [(1, "try 1"), (2, "try 2"), (3, "try 3")]


@pytest.mark.asyncio
async def test_delay_is_capped_at_max_delay(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("herd_common.retry.asyncio.sleep", fake_sleep)

    async def fn() -> None:
        raise _Transient("nope")

    with pytest.raises(_Transient):
        await retry_with_backoff(fn, attempts=5, initial_delay=1.0, factor=10.0, max_delay=2.0)

    assert sleeps == [1.0, 2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_invalid_attempts_raises_value_error():
    async def fn() -> None:
        return None

    with pytest.raises(ValueError):
        await retry_with_backoff(fn, attempts=0)
