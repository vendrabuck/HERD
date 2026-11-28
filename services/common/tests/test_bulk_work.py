"""Unit tests for `herd_common.bulk_work.run_bulk_work` (issue #1417).

The importers' parse and the exports' rendering run through it. What it promises:
the work runs on a worker thread, never the event loop's; never on asyncio's
default executor (the one `loop.getaddrinfo` uses, issue #1416); at most
`BULK_WORK_THREADS` at once; a result and an exception come back unchanged; and a
cancelled caller keeps its slot until its thread ends.

Every overlap is staged with events, never a sleep: a worker blocks on a
`threading.Event` the test sets, and the test waits for a worker's signal off the
loop (`asyncio.to_thread(event.wait, timeout)`), each wait bounded and failing the
test when it expires.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import threading

import pytest
from fastapi import HTTPException
from herd_common import bulk_work
from herd_common.bulk_work import BULK_WORK_THREADS, bulk_work_limiter, run_bulk_work

_WAIT = 10.0


async def _signalled(event: threading.Event) -> bool:
    return await asyncio.to_thread(event.wait, _WAIT)


async def _until(predicate) -> None:
    """Yield to the loop until ``predicate()`` holds, failing after ``_WAIT``."""
    async with asyncio.timeout(_WAIT):
        while not predicate():
            await asyncio.sleep(0)


class _RefusingExecutor(concurrent.futures.ThreadPoolExecutor):
    """A default executor that fails any work handed to it."""

    def submit(self, *args, **kwargs):
        raise AssertionError("work was handed to the default executor")


@pytest.fixture
def fresh_limiter(monkeypatch):
    """A limiter of this test's own, so a slot one test holds never reaches another."""
    monkeypatch.setattr(bulk_work, "_limiter", None)
    monkeypatch.setattr(bulk_work, "_workers", None)


def test_two_threads_at_most():
    assert BULK_WORK_THREADS == 2


@pytest.mark.usefixtures("fresh_limiter")
async def test_the_work_runs_on_a_worker_thread_and_returns_its_result():
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def work(a, b):
        seen.append(threading.get_ident())
        return a + b

    assert await run_bulk_work(work, 2, 3) == 5
    assert seen and seen[0] != loop_thread


@pytest.mark.usefixtures("fresh_limiter")
async def test_an_exception_comes_back_unchanged():
    refusal = HTTPException(status_code=422, detail="fixed")

    def work():
        raise refusal

    with pytest.raises(HTTPException) as caught:
        await run_bulk_work(work)
    assert caught.value is refusal


@pytest.mark.usefixtures("fresh_limiter")
async def test_the_work_never_takes_the_default_executor():
    """Mutation-checked: running the work through `asyncio.to_thread` fails this."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(_RefusingExecutor())
    try:
        assert await run_bulk_work(lambda: "done") == "done"
    finally:
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor())


@pytest.mark.usefixtures("fresh_limiter")
async def test_at_most_the_bound_runs_at_once_and_a_third_waits():
    """Mutation-checked: taking no slot and handing anyio its default limiter (40
    threads) fails this."""
    entered = [threading.Event() for _ in range(BULK_WORK_THREADS + 1)]
    release = threading.Event()

    def work(index):
        entered[index].set()
        assert release.wait(_WAIT), "never released"
        return index

    tasks = [asyncio.create_task(run_bulk_work(work, i)) for i in range(len(entered))]
    try:
        for index in range(BULK_WORK_THREADS):
            assert await _signalled(entered[index]), f"worker {index} never started"
        limiter = bulk_work_limiter()
        await _until(lambda: limiter.statistics().tasks_waiting == 1)
        assert limiter.borrowed_tokens == BULK_WORK_THREADS
        assert not entered[BULK_WORK_THREADS].is_set()
    finally:
        release.set()
    assert await asyncio.gather(*tasks) == list(range(len(entered)))
    assert entered[BULK_WORK_THREADS].is_set()
    assert bulk_work_limiter().borrowed_tokens == 0


@pytest.mark.usefixtures("fresh_limiter")
async def test_a_cancelled_caller_keeps_its_slot_until_its_thread_ends():
    """The caller sees its cancellation at once; the slot comes back only when the
    thread ends. Mutation-checked: releasing the slot when the caller leaves, rather
    than in the work's done callback, fails this."""
    entered = threading.Event()
    release = threading.Event()

    def work():
        entered.set()
        assert release.wait(_WAIT), "never released"

    task = asyncio.create_task(run_bulk_work(work))
    try:
        assert await _signalled(entered), "the worker never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bulk_work_limiter().borrowed_tokens == 1
    finally:
        release.set()
    await _until(lambda: bulk_work_limiter().borrowed_tokens == 0)


@pytest.mark.usefixtures("fresh_limiter")
async def test_a_caller_cancelled_while_it_waits_takes_no_slot_and_runs_nothing():
    entered = [threading.Event() for _ in range(BULK_WORK_THREADS)]
    release = threading.Event()
    ran: list[str] = []

    def hold(index):
        entered[index].set()
        assert release.wait(_WAIT), "never released"

    holders = [asyncio.create_task(run_bulk_work(hold, i)) for i in range(BULK_WORK_THREADS)]
    try:
        for event in entered:
            assert await _signalled(event), "a holder never started"
        waiter = asyncio.create_task(run_bulk_work(ran.append, "waiter"))
        limiter = bulk_work_limiter()
        await _until(lambda: limiter.statistics().tasks_waiting == 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert limiter.statistics().tasks_waiting == 0
    finally:
        release.set()
    await asyncio.gather(*holders)
    await _until(lambda: bulk_work_limiter().borrowed_tokens == 0)
    assert ran == []


@pytest.mark.usefixtures("fresh_limiter")
async def test_anyio_is_handed_the_modules_own_worker_limiter(monkeypatch):
    """Never anyio's shared default limiter (40 threads, also Starlette's).
    Mutation-checked: `limiter=None` fails this."""
    handed: list[object] = []
    real_run_sync = bulk_work.anyio.to_thread.run_sync

    async def recording(func, *args, limiter=None, **kwargs):
        handed.append(limiter)
        return await real_run_sync(func, *args, limiter=limiter, **kwargs)

    monkeypatch.setattr(bulk_work.anyio.to_thread, "run_sync", recording)
    assert await run_bulk_work(lambda: 1) == 1
    assert handed == [bulk_work._worker_limiter()]
    assert handed[0] is not bulk_work.anyio.to_thread.current_default_thread_limiter()


@pytest.mark.usefixtures("fresh_limiter")
async def test_work_that_fails_after_its_caller_left_logs_nothing():
    """The done callback retrieves the work's exception, so asyncio never reports
    it as never retrieved. Mutation-checked: dropping `done.exception()` fails this."""
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    entered = threading.Event()
    release = threading.Event()

    def work():
        entered.set()
        assert release.wait(_WAIT), "never released"
        raise RuntimeError("after the caller left")

    try:
        task = asyncio.create_task(run_bulk_work(work))
        assert await _signalled(entered), "the worker never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await _until(lambda: bulk_work_limiter().borrowed_tokens == 0)
        del task
        gc.collect()
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        release.set()
        loop.set_exception_handler(previous)
    assert [c.get("message") for c in reported] == []


async def test_the_limiter_is_one_per_process(monkeypatch):
    monkeypatch.setattr(bulk_work, "_limiter", None)
    first = bulk_work_limiter()
    assert bulk_work_limiter() is first
    assert first.total_tokens == BULK_WORK_THREADS
