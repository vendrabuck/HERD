"""The ONE way bulk import and export work leaves the event loop (issue #1417).

Inventory's device and template importers and cabling's topology importer decode an
uploaded file of up to 10 MiB, parse it as CSV or JSON, and scan it for lone
surrogates before any row is judged; their exports render every record into one
CSV or JSON body. Each is pure CPU work with no database access, and on the event
loop it held the loop for as long as it ran (measured on one core: about 120 ms for
a 10 MiB JSON file and about 300 ms for a 10 MiB CSV file of small rows), so every
other request the service had in flight waited behind it.

``run_bulk_work(func, *args)`` runs ``func(*args)`` on a worker thread and returns
its result, or raises what it raised (an ``HTTPException`` included, unchanged). The
threads:

- are anyio's worker threads (``anyio.to_thread.run_sync``), never asyncio's default
  executor: that executor is the one ``loop.getaddrinfo`` resolves names on (new
  database and NATS connections), which issue #1416 found already shared with
  inventory's driver storage calls, so bulk work does not add to it;
- take a slot of this module's own limiter of ``BULK_WORK_THREADS`` (2), never of
  anyio's shared default limiter (40 threads, which Starlette also uses for sync
  dependencies and file reads), so at most two bulk parses or renders run in a
  process at once, whichever routes and services they come from; a third waits for
  a slot, with no bound on the wait, holding no database connection unless its
  caller already opened one;
- are not abandoned on cancellation: the slot is taken before the thread starts
  and given back only when the thread ends, so a request cancelled while its work
  runs (the caller sees the cancellation at once) does not let another thread
  start beside one still running, and the bound holds whatever is cancelled. A
  request cancelled while it waits for a slot takes none, and its work never runs.

The work still holds the GIL while it runs; the event loop gets the interpreter back
at every switch interval (5 ms by default), so other requests are served
concurrently, at some cost in latency, instead of waiting for the whole parse.

The limiters are built on first use and are process-wide: each service runs one uvicorn
process with one event loop. Database work stays on the event loop in the request's
own session; never pass a session or a lazy-loading ORM object to ``run_bulk_work``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

import anyio
import anyio.to_thread

# At most this many bulk parses or renders run at once in one process. Each holds the
# GIL while it runs, so more threads add no throughput, and each holds a parsed copy
# of an upload of up to 10 MiB, so the bound also bounds that memory.
BULK_WORK_THREADS = 2

_T = TypeVar("_T")

# The slots a caller takes before its thread starts, given back when the thread ends;
# and the limiter handed to anyio for the thread itself, never anyio's shared default
# one. A caller holding a slot never waits for a worker token.
_limiter: anyio.CapacityLimiter | None = None
_workers: anyio.CapacityLimiter | None = None


def bulk_work_limiter() -> anyio.CapacityLimiter:
    """The process-wide slots every ``run_bulk_work`` call takes one of."""
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(BULK_WORK_THREADS)
    return _limiter


def _worker_limiter() -> anyio.CapacityLimiter:
    global _workers
    if _workers is None:
        _workers = anyio.CapacityLimiter(BULK_WORK_THREADS)
    return _workers


async def run_bulk_work(func: Callable[..., _T], *args: object) -> _T:
    """``func(*args)`` on a bulk worker thread; see the module docstring."""
    slots = bulk_work_limiter()
    borrower = object()
    await slots.acquire_on_behalf_of(borrower)
    try:
        work = asyncio.ensure_future(
            anyio.to_thread.run_sync(func, *args, limiter=_worker_limiter())
        )
    except BaseException:
        slots.release_on_behalf_of(borrower)
        raise

    def finished(done: asyncio.Future) -> None:
        slots.release_on_behalf_of(borrower)
        # Retrieved here, so work whose caller was cancelled logs nothing.
        if not done.cancelled():
            done.exception()

    work.add_done_callback(finished)
    return await asyncio.shield(work)


__all__ = ["BULK_WORK_THREADS", "bulk_work_limiter", "run_bulk_work"]
