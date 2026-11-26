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

Adopted by reservations' lifespan and the outbox relay's wake-up listener.
Execution's three `_surface_crash` copies, the consumer schema gate's, and
inventory's `_log_scheduler_crash` predate it and move to it in a later cleanup.
"""

import asyncio
import logging

TASK_CRASHED_ACTION = "background_task_crashed"


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


__all__ = ["TASK_CRASHED_ACTION", "log_task_crash"]
