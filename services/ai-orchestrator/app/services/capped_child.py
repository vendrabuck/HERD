"""Run an untrusted-input parser in a child process the kernel caps (issue #1202).

The parent side of the capped child. The child (for a PDF, ``_pdf_child.py``) applies
its own resource limits before it reads the input or imports the parser, so the
memory and CPU it can use are bounded by the operating system on every code path of
the parser, found or not. This module bounds everything the parent itself spends on
the child, whatever the child does:

- it starts the child with ``sys.executable -I -B`` (isolated mode, no bytecode, the
  flags the execution service's driver sandbox uses, issue #1180), an empty
  environment (no setting, token, or key of the service reaches it), no descriptors
  but the three pipes (``close_fds``, the default), and in a new session, so the child
  leads its own process group;
- it feeds the input on stdin, keeps at most ``max_stdout_bytes`` of stdout and kills
  the child the moment it writes one byte more, and keeps at most
  ``MAX_STDERR_BYTES`` of stderr the same way: no child output can grow the
  parent's memory;
- it enforces a wall-clock deadline from the moment the child starts;
- on EVERY path out (the child exits, the deadline passes, an output cap is passed,
  the awaiting task is cancelled) it sends SIGKILL to the whole process group, so
  nothing the child started outlives the read, and reaps the child, so no zombie and
  no open pipe is left;
- it reads the outcome only from the exit status, the signal that ended the child,
  and one fixed-format status line, the last line of stderr, matched strictly.

Concurrency is capped by named pools (``acquire``, ``acquire_slot``): a fixed number of
slots, a bounded wait, a bounded number of waiters, and optionally one slot per owner,
so the number of children, and so their combined memory ceiling, is fixed. The
extractor uses a second pool for the reads it does in process.
"""

import asyncio
import contextlib
import enum
import os
import re
import signal
import sys
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field

# What the parent keeps of a child's stderr: the status line is short; an honest child
# writes nothing else there (it switches logging and warnings off), so a child that
# writes more than this is killed and its result is malformed.
MAX_STDERR_BYTES = 64 * 1024

_READ_CHUNK_BYTES = 64 * 1024


class Outcome(enum.Enum):
    """How a capped child ended, read from its exit status, signal, and status line."""

    OK = "ok"
    PARSE_ERROR = "parse-error"
    MEMORY = "memory"
    # Ended by a signal the parent did not send: the kernel at a limit (SIGKILL from
    # the RLIMIT_CPU hard limit or the out-of-memory killer) or C code whose allocation
    # failed under RLIMIT_AS (SIGSEGV, SIGABRT, SIGBUS). Which of these it was cannot
    # be told from the status alone.
    KILLED = "killed"
    CPU = "cpu"
    DEADLINE = "deadline"
    UNAVAILABLE = "unavailable"
    # The parent's own failures, never caused by the input alone: the child could
    # not be started, could not apply its limits, or broke the protocol (a status
    # line missing or malformed, a status that disagrees with the exit code, an output
    # cap passed, or a signal no limit sends).
    START_FAILED = "start-failed"
    LIMITS_FAILED = "limits-failed"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ChildResult:
    outcome: Outcome
    stdout: bytes = b""
    # The exception class name a parse error reports; matched against a strict
    # identifier pattern, so it is never file text.
    class_name: str | None = None


# The signals that mean the child ran out of a limit. SIGXCPU is RLIMIT_CPU's soft
# limit. SIGKILL that the parent did not send is the RLIMIT_CPU hard limit or the
# kernel's out-of-memory killer, and SIGSEGV, SIGABRT, and SIGBUS are how C code (the
# interpreter's allocator included) can end when an allocation fails under RLIMIT_AS:
# all four are Outcome.KILLED, memory or the hard CPU limit, not told apart.
_CPU_SIGNALS = frozenset({signal.SIGXCPU})
_KILLED_SIGNALS = frozenset({signal.SIGKILL, signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS})


def _status_pattern(prefix: str) -> re.Pattern[bytes]:
    return re.compile(
        re.escape(prefix.encode("ascii"))
        + rb" (ok|memory|unavailable|limits-failed|usage|parse-error [A-Za-z_][A-Za-z0-9_]{0,99})"
    )


def _killpg(pgid: int) -> None:
    """SIGKILL the child's process group; a group already gone is fine."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


async def _feed(stream: asyncio.StreamWriter, data: bytes) -> None:
    """Write the input and close stdin. A child that exits without reading it all
    (one refused before the read) closes the pipe; that is not the parent's error."""
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stream.write(data)
        await stream.drain()
    # Closed without awaiting wait_closed(): cancelling that await (the deadline, a
    # cancelled request) cancels the protocol's own close future, and asyncio then
    # fails setting its result when the pipe closes ("invalid state", logged by the
    # loop). The cleanup reaps the child; nothing here needs the close confirmed.
    stream.close()


async def _read_bounded(stream: asyncio.StreamReader, cap: int, on_over) -> tuple[bytes, bool]:
    """Read to EOF, keeping at most `cap` bytes. Past `cap`, call `on_over` (which
    kills the child), then read on to EOF discarding every byte: returns (the first
    `cap` bytes, True).

    The discarding read is what lets the child be reaped. A stream nobody reads pauses
    its pipe transport once its buffer is full, a paused transport never sees EOF, and
    asyncio's ``Process.wait()`` returns only after every pipe has disconnected, so
    stopping at the cap left ``wait()`` hanging until the deadline whenever the child
    had written enough before the kill landed (seen on a CI runner as a deadline where
    the cap was due). The killed child closes its end at once, so the drain is short,
    and it keeps nothing.
    """
    chunks: list[bytes] = []
    kept = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks), False
        if kept + len(chunk) > cap:
            chunks.append(chunk[: cap - kept])
            on_over()
            while await stream.read(_READ_CHUNK_BYTES):
                pass
            return b"".join(chunks), True
        chunks.append(chunk)
        kept += len(chunk)


async def _exited(pid: int) -> None:
    """Return once the process `pid` has exited, whether or not it is reaped yet.

    ``Process.wait()`` is not enough: asyncio wakes it only after the child has exited
    AND every pipe has closed, so a grandchild that holds a pipe open would keep it
    waiting until the deadline. A pidfd becomes readable when the process exits, pipes
    or not (Linux 5.3 and later, Python 3.9 and later). A platform without it raises
    OSError here, which the caller reads as a child that could not start: fail closed.
    """
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def on_readable() -> None:
        if not done.done():
            done.set_result(None)

    loop.add_reader(pidfd, on_readable)
    try:
        await done
    finally:
        loop.remove_reader(pidfd)
        os.close(pidfd)


def classify(
    returncode: int,
    stderr: bytes,
    status_prefix: str,
    exit_codes: dict[str, int],
) -> tuple[Outcome, str | None]:
    """The outcome of a child that ended on its own (not by the parent's kill)."""
    if returncode < 0:
        sig = -returncode
        if sig in _CPU_SIGNALS:
            return Outcome.CPU, None
        if sig in _KILLED_SIGNALS:
            return Outcome.KILLED, None
        return Outcome.MALFORMED, None
    lines = [line for line in stderr.split(b"\n") if line.strip()]
    if not lines:
        return Outcome.MALFORMED, None
    match = _status_pattern(status_prefix).fullmatch(lines[-1])
    if match is None:
        return Outcome.MALFORMED, None
    word, _, class_name = match.group(1).decode("ascii").partition(" ")
    if exit_codes.get(word) != returncode:
        return Outcome.MALFORMED, None
    if word == "parse-error":
        return Outcome.PARSE_ERROR, class_name
    if word == "usage":
        return Outcome.MALFORMED, None
    return Outcome(word), None


# Cleanup tasks of children whose start was cancelled, kept referenced until done.
_BACKGROUND: set[asyncio.Task] = set()


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    _killpg(proc.pid)
    if proc.stdin is not None:
        proc.stdin.close()
    await proc.wait()


def _reap_after_cancelled_start(spawn: asyncio.Future) -> None:
    """The request was cancelled while the child was starting. The start is left to
    finish (cancelling it inside asyncio's pipe setup leaves a future whose
    CancelledError nobody retrieves), and then the child is killed and reaped."""
    if spawn.cancelled() or spawn.exception() is not None:
        return
    task = asyncio.ensure_future(_kill_and_reap(spawn.result()))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _uncancellable(awaitable: asyncio.Future) -> None:
    """Await `awaitable` to its end whatever cancellations arrive meanwhile, then
    raise CancelledError if any did."""
    cancelled = False
    while not awaitable.done():
        try:
            await asyncio.shield(awaitable)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def run_capped_child(
    script: str,
    args: list[str],
    stdin: bytes,
    *,
    wall_seconds: float,
    max_stdout_bytes: int,
    status_prefix: str,
    exit_codes: dict[str, int],
) -> ChildResult:
    """Run ``sys.executable -I -B script *args`` with ``stdin`` as its input, under
    the parent-side bounds in the module docstring, and return how it ended."""
    spawn = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            script,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
            cwd="/",
            start_new_session=True,
        )
    )
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        spawn.add_done_callback(_reap_after_cancelled_start)
        raise
    except OSError:
        return ChildResult(Outcome.START_FAILED)

    pgid = proc.pid
    over_cap = False

    def kill_for_overflow() -> None:
        nonlocal over_cap
        over_cap = True
        _killpg(pgid)

    tasks: list[asyncio.Future] = []

    async def cleanup() -> None:
        _killpg(pgid)
        for task in tasks:
            task.cancel()
        if proc.returncode is None:
            # SIGKILL cannot be caught, so this wait is short.
            await proc.wait()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        try:
            async with asyncio.timeout(wall_seconds):
                assert proc.stdin and proc.stdout and proc.stderr
                tasks = [
                    asyncio.ensure_future(_feed(proc.stdin, stdin)),
                    asyncio.ensure_future(
                        _read_bounded(proc.stdout, max_stdout_bytes, kill_for_overflow)
                    ),
                    asyncio.ensure_future(
                        _read_bounded(proc.stderr, MAX_STDERR_BYTES, kill_for_overflow)
                    ),
                ]
                await _exited(proc.pid)
                # The child is gone; anything it started may still hold the pipes open.
                _killpg(pgid)
                returncode = await proc.wait()
                await tasks[0]
                stdout, _ = await tasks[1]
                stderr, _ = await tasks[2]
        except TimeoutError:
            return ChildResult(Outcome.DEADLINE)
        except OSError:
            # pidfd_open is missing or refused; the cleanup kills and reaps the child.
            return ChildResult(Outcome.START_FAILED)
        if over_cap:
            return ChildResult(Outcome.MALFORMED)
        outcome, class_name = classify(returncode, stderr, status_prefix, exit_codes)
        if outcome is not Outcome.OK:
            return ChildResult(outcome, class_name=class_name)
        return ChildResult(Outcome.OK, stdout=stdout)
    finally:
        # Every path: deadline, cancellation, an error in this function, success. The
        # cleanup runs to its end even if the caller is cancelled again meanwhile, so
        # the child is reaped and every pipe task finished before this returns.
        await _uncancellable(asyncio.ensure_future(cleanup()))


# --- Concurrency caps ---


class ReaderBusy(Exception):
    """No slot came free within the wait, the waiting line is full, or the owner
    already holds or waits for a slot of the same pool."""


@dataclass
class _Pool:
    limit: int
    slots: asyncio.Semaphore
    waiting: int = 0
    owners: set = field(default_factory=set)


# One set of pools per event loop: asyncio primitives bind to the loop that first
# waits on them, and the tests run many loops in one process. The service runs one
# loop, so there each pool is one service-wide cap.
_POOLS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Pool]]" = (
    weakref.WeakKeyDictionary()
)


def _pool(name: str, limit: int) -> _Pool:
    pools = _POOLS.setdefault(asyncio.get_running_loop(), {})
    if name not in pools:
        pools[name] = _Pool(limit=limit, slots=asyncio.Semaphore(limit))
    return pools[name]


def pool_state(name: str) -> tuple[int, int, int, frozenset] | None:
    """(free slots, limit, waiting, owners) of the running loop's pool `name`, or None
    before its first use. For tests and diagnostics."""
    pool = _POOLS.get(asyncio.get_running_loop(), {}).get(name)
    if pool is None:
        return None
    return pool.slots._value, pool.limit, pool.waiting, frozenset(pool.owners)


async def acquire(
    name: str,
    limit: int,
    wait_seconds: float,
    max_waiters: int,
    owner: object | None = None,
) -> Callable[[], None]:
    """Take one of `limit` slots of pool `name` and return the function that gives it
    back (idempotent). Raise ReaderBusy at once when `owner` already holds or waits for
    a slot of this pool, or when no slot is free and `max_waiters` already wait; after
    `wait_seconds` without a slot. The owner mark and the waiting count are undone on
    every way out, cancellation included."""
    pool = _pool(name, limit)
    if owner is not None:
        if owner in pool.owners:
            raise ReaderBusy
        pool.owners.add(owner)
    try:
        if pool.slots.locked() and pool.waiting >= max_waiters:
            raise ReaderBusy
        pool.waiting += 1
        try:
            async with asyncio.timeout(wait_seconds):
                await pool.slots.acquire()
        except TimeoutError:
            raise ReaderBusy from None
        finally:
            pool.waiting -= 1
    except BaseException:
        if owner is not None:
            pool.owners.discard(owner)
        raise

    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        pool.slots.release()
        if owner is not None:
            pool.owners.discard(owner)

    return release


@contextlib.asynccontextmanager
async def acquire_slot(
    name: str,
    limit: int,
    wait_seconds: float,
    max_waiters: int,
    owner: object | None = None,
):
    """Hold one slot of pool `name` while the body runs (see ``acquire``)."""
    release = await acquire(name, limit, wait_seconds, max_waiters, owner)
    try:
        yield
    finally:
        release()
