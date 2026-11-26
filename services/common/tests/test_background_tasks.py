"""`herd_common.background_tasks.log_task_crash` (issue #1549).

The ONE done callback that logs a background task the moment it ends with an
exception, by class name only. Every wait here is bounded by event-loop
iterations (`asyncio.sleep(0)`), never a clock.
"""

import asyncio
import logging

import pytest
from herd_common.background_tasks import TASK_CRASHED_ACTION, log_task_crash

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
