"""`herd_common.background_tasks` (issue #1549).

`log_task_crash`, the ONE done callback that logs a background task the moment it
ends with an exception, by class name only; and `await_stopped_task`, the shutdown
half that awaits a cancelled task and returns, never raises, what it ended with.
Every wait here is bounded by event-loop iterations (`asyncio.sleep(0)`), never a
clock.
"""

import asyncio
import logging

import pytest
from herd_common.background_tasks import (
    TASK_CRASHED_ACTION,
    TASK_FAILED_AT_SHUTDOWN_ACTION,
    await_stopped_task,
    log_task_crash,
)

_LOG = logging.getLogger("herd_common.tests.background_tasks")
# Text an exception might carry from a driver, a model, or a payload: it must never
# reach the log line.
_EXCEPTION_TEXT = "device said: % Invalid input at 'show running-config'"


async def _spin(iterations: int = 20) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


class _TaskDied(Exception):
    pass


def _crash_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "action", None) == TASK_CRASHED_ACTION]


async def test_a_task_that_raises_is_logged_when_it_ends_by_class_name_only(caplog):
    async def dies():
        raise _TaskDied(_EXCEPTION_TEXT)

    with caplog.at_level(logging.ERROR, logger=_LOG.name):
        task = asyncio.create_task(dies())
        assert log_task_crash(task, name="sweep_loop", log=_LOG) is task
        await _spin()
        assert task.done()

        records = _crash_records(caplog)
        assert len(records) == 1
        (record,) = records
        assert record.levelno == logging.ERROR
        assert record.name == _LOG.name
        assert record.task_name == "sweep_loop"
        assert record.error_class == "_TaskDied"
        assert record.getMessage() == "Background task sweep_loop ended with _TaskDied"
        assert record.exc_info is None
        assert _EXCEPTION_TEXT not in caplog.text

    # The exception stays on the task: a later await still raises it.
    with pytest.raises(_TaskDied):
        await task


@pytest.mark.parametrize("ending", ["cancelled", "returned"])
async def test_a_task_that_is_cancelled_or_returns_is_not_logged(caplog, ending):
    async def runs():
        if ending == "returned":
            return 7
        await asyncio.Event().wait()

    with caplog.at_level(logging.DEBUG, logger=_LOG.name):
        task = log_task_crash(asyncio.create_task(runs()), name="sweep_loop", log=_LOG)
        await _spin()
        if ending == "cancelled":
            task.cancel()
            await _spin()
            assert task.cancelled()
        else:
            assert task.result() == 7

    assert caplog.records == []


# --- await_stopped_task: the shutdown half (issue #1549) -----------------------------


class _TaskDiedHard(BaseException):
    """Not an `Exception`, and not one asyncio treats as a process-exit signal."""


class _ThrownIntoCaller(BaseException):
    """Thrown into the awaiting coroutine from outside; never the task's exception."""


def _shutdown_records(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if getattr(r, "action", None) == TASK_FAILED_AT_SHUTDOWN_ACTION
    ]


@pytest.mark.parametrize("ending", ["cancelled", "returned"])
async def test_await_stopped_task_returns_none_for_a_cancelled_or_returned_task(caplog, ending):
    """A task that ended cancelled (what its owner asked for) or returned gives None,
    raises nothing, and logs nothing."""

    async def runs():
        if ending == "returned":
            return 7
        await asyncio.Event().wait()

    task = asyncio.create_task(runs())
    await _spin()
    if ending == "cancelled":
        task.cancel()
    with caplog.at_level(logging.DEBUG, logger=_LOG.name):
        assert await await_stopped_task(task, name="sweep_loop", log=_LOG) is None
    assert caplog.records == []


@pytest.mark.parametrize("died_with", [_TaskDied, _TaskDiedHard])
async def test_await_stopped_task_returns_a_dead_tasks_exception_and_logs_it_once(
    caplog, died_with
):
    """A task that ended with its own exception, an `Exception` or another
    `BaseException`, gives that very exception back instead of raising it, and the
    helper logs exactly one ERROR line: the fixed `action`, the task's name, and the
    class name, never the exception's text."""

    async def dies():
        raise died_with(_EXCEPTION_TEXT)

    task = asyncio.create_task(dies())
    await _spin()
    with caplog.at_level(logging.DEBUG, logger=_LOG.name):
        ended_with = await await_stopped_task(task, name="sweep_loop", log=_LOG)

    assert ended_with is task.exception()
    assert isinstance(ended_with, died_with)
    records = _shutdown_records(caplog)
    assert [(r.levelname, r.task_name, r.error_class) for r in records] == [
        ("ERROR", "sweep_loop", died_with.__name__)
    ]
    assert records[0].getMessage() == (
        f"Background task sweep_loop had ended with {died_with.__name__}; shutdown continues"
    )
    assert records[0].exc_info is None
    assert len(caplog.records) == 1
    assert _EXCEPTION_TEXT not in caplog.text


async def test_an_exception_thrown_into_the_caller_propagates_even_after_the_task_died(caplog):
    """The identity check: the helper is suspended on task T, T then ends with its own
    ValueError, and before the helper resumes an X is thrown into it. X is not T's
    exception (T is done and not cancelled, but holds another object), so X propagates
    and nothing is logged; it is never returned as if T had ended with it."""
    release = asyncio.Event()

    async def dies_on_release():
        await release.wait()
        raise ValueError(_EXCEPTION_TEXT)

    task = asyncio.create_task(dies_on_release())
    await _spin()
    waiter = await_stopped_task(task, name="sweep_loop", log=_LOG)
    with caplog.at_level(logging.DEBUG, logger=_LOG.name):
        assert waiter.send(None) is task  # suspended on T
        release.set()
        await _spin()
        assert task.done() and isinstance(task.exception(), ValueError)
        with pytest.raises(_ThrownIntoCaller):
            waiter.throw(_ThrownIntoCaller())
    assert _shutdown_records(caplog) == []
