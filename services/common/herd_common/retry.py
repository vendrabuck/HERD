"""Async retry with exponential backoff.

Usage:
    from herd_common.retry import retry_with_backoff

    await retry_with_backoff(
        lambda: httpx_client.post(url, json=payload),
        attempts=3,
        initial_delay=0.5,
    )

The callable must be a zero-arg awaitable factory; it is re-invoked on each attempt.
Non-retryable exception types bypass the retry and raise immediately.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    initial_delay: float = 0.5,
    factor: float = 2.0,
    max_delay: float = 10.0,
    retryable: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retryable as exc:
            last_exc = exc
            if on_retry is not None:
                on_retry(attempt, exc)
            if attempt >= attempts:
                break
            await asyncio.sleep(min(delay, max_delay))
            delay *= factor

    assert last_exc is not None
    raise last_exc
