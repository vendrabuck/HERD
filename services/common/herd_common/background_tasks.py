"""The ONE done callback that logs a background task that ended with an exception.

Issue #1549. A long-lived task a service starts in its lifespan (a sweep loop, an
outbox relay) runs until shutdown cancels it. When one ends with an exception
instead, nothing awaits it until shutdown, so without a done callback the failure
is silent until then and the service goes on answering requests with the task
gone. `log_task_crash` attaches the callback that logs it the moment the task
ends.

The log line names the task and the exception's CLASS only: an exception's text
can quote a driver, a model, a payload, or a stored value, and none of those may
reach a log line (class-name-only errors). A task that ends cancelled or returns
normally is not logged here.

`await_stopped_task` is the shutdown half: it awaits a task its owner cancelled and
returns, never raises, what the task ended with (any `BaseException`), so one dead
task cannot stop the owner's remaining shutdown steps.

Adopted by reservations' lifespan and the outbox relay's wake-up listener.
Execution's three `_surface_crash` copies, the consumer schema gate's, and
inventory's `_log_scheduler_crash` predate it and move to it in a later cleanup.
"""

import asyncio
import logging

TASK_CRASHED_ACTION = "background_task_crashed"
TASK_FAILED_AT_SHUTDOWN_ACTION = "background_task_failed_at_shutdown"


def log_task_crash(task: asyncio.Task, *, name: str, log: logging.Logger) -> asyncio.Task:
    """Log `task` at ERROR on `log` when it ends with an exception; return `task`.

    The record carries `action` (`TASK_CRASHED_ACTION`), `task_name`, and
    `error_class`. Reading the exception in the callback also marks it retrieved,
    so asyncio does not report it a second time when the task is collected; a
    later `await task` still raises it.
    """

    def _on_done(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc is None:
            return
        log.error(
            "Background task %s ended with %s",
            name,
            type(exc).__name__,
            extra={
                "action": TASK_CRASHED_ACTION,
                "task_name": name,
                "error_class": type(exc).__name__,
            },
        )

    task.add_done_callback(_on_done)
    return task


async def await_stopped_task(
    task: asyncio.Task, *, name: str, log: logging.Logger
) -> BaseException | None:
    """Await a task its owner has cancelled at shutdown; never raise what it ended with.

    Returns None when the task ended cancelled or returned. When it ended with an
    exception of its own, any `BaseException` (a `KeyboardInterrupt`, a
    `SystemExit`, a `BaseException` subclass, as well as an `Exception`), that
    exception is logged at ERROR (`action` `TASK_FAILED_AT_SHUTDOWN_ACTION`, the task's
    name, the class name only) and RETURNED, not raised, so the owner's remaining
    shutdown steps still run; the owner decides whether to re-raise it after them.

    What is thrown into the CALLER while it waits is not the task's and propagates at
    once: a `GeneratorExit` when the calling coroutine is closed while suspended here
    (handling it by awaiting again would raise "coroutine ignored GeneratorExit"). An
    exception counts as the task's only when the task is done, not cancelled, and
    holds that very object. A `CancelledError` is returned as None whether the task
    ended cancelled or the caller itself was cancelled while waiting (the two raise
    the same error here); a caller that must tell them apart reads its own
    `asyncio.current_task().cancelling()`.
    """
    try:
        await task
    except asyncio.CancelledError:
        return None
    except BaseException as exc:
        if not (task.done() and not task.cancelled() and task.exception() is exc):
            raise
        log.error(
            "Background task %s had ended with %s; shutdown continues",
            name,
            type(exc).__name__,
            extra={
                "action": TASK_FAILED_AT_SHUTDOWN_ACTION,
                "task_name": name,
                "error_class": type(exc).__name__,
            },
        )
        return exc
    return None


__all__ = [
    "TASK_CRASHED_ACTION",
    "TASK_FAILED_AT_SHUTDOWN_ACTION",
    "await_stopped_task",
    "log_task_crash",
]
