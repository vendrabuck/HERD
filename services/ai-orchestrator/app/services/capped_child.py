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

Concurrent children are capped by ``acquire_slot``: a module-wide limit with a bounded
wait, so the number of children, and so their combined memory ceiling, is fixed.
"""

import asyncio
import contextlib
import enum
import os
import re
import signal
import sys
import weakref
from dataclasses import dataclass

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
# kernel's out-of-memory killer; the child does not catch SIGXCPU, so the soft limit
# ends it first and an unexplained SIGKILL is read as memory. SIGSEGV, SIGABRT, and
# SIGBUS are how C code (the interpreter's allocator included) can end when an
# allocation fails under RLIMIT_AS.
_CPU_SIGNALS = frozenset({signal.SIGXCPU})
_MEMORY_SIGNALS = frozenset({signal.SIGKILL, signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS})


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
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stream.close()
        await stream.wait_closed()


async def _read_bounded(stream: asyncio.StreamReader, cap: int, on_over) -> tuple[bytes, bool]:
    """Read to EOF, keeping at most `cap` bytes. Past `cap`, call `on_over` (which
    kills the child) and stop reading at once: returns (the first `cap` bytes, True)."""
    chunks: list[bytes] = []
    kept = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks), False
        if kept + len(chunk) > cap:
            chunks.append(chunk[: cap - kept])
            on_over()
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
        if sig in _MEMORY_SIGNALS:
            return Outcome.MEMORY, None
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
    try:
        proc = await asyncio.create_subprocess_exec(
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
    except OSError:
        return ChildResult(Outcome.START_FAILED)

    pgid = proc.pid
    over_cap = False

    def kill_for_overflow() -> None:
        nonlocal over_cap
        over_cap = True
        _killpg(pgid)

    tasks: list[asyncio.Task] = []
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
            # pidfd_open is missing or refused; the finally kills and reaps the child.
            return ChildResult(Outcome.START_FAILED)
        if over_cap:
            return ChildResult(Outcome.MALFORMED)
        outcome, class_name = classify(returncode, stderr, status_prefix, exit_codes)
        if outcome is not Outcome.OK:
            return ChildResult(outcome, class_name=class_name)
        return ChildResult(Outcome.OK, stdout=stdout)
    finally:
        # Every path: deadline, cancellation, an error in this function, success.
        _killpg(pgid)
        for task in tasks:
            task.cancel()
        if proc.returncode is None:
            # SIGKILL cannot be caught, so this wait is short. It is shielded: a
            # second cancellation ends this await, not the wait, and the loop's child
            # watcher reaps the child either way.
            await asyncio.shield(proc.wait())
        await asyncio.gather(*tasks, return_exceptions=True)


# --- Concurrency cap ---


class ReaderBusy(Exception):
    """No capped-child slot came free within the wait."""


# One semaphore per event loop: asyncio primitives bind to the loop that first waits
# on them, and the tests run many loops in one process. The service runs one loop, so
# there it is one module-wide cap.
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


@contextlib.asynccontextmanager
async def acquire_slot(limit: int, wait_seconds: float):
    """Hold one of `limit` slots while the body runs; wait at most `wait_seconds` for
    one, then raise ReaderBusy. ``limit`` is read on the loop's first use."""
    loop = asyncio.get_running_loop()
    slots = _SLOTS.get(loop)
    if slots is None:
        slots = _SLOTS[loop] = asyncio.Semaphore(limit)
    try:
        async with asyncio.timeout(wait_seconds):
            await slots.acquire()
    except TimeoutError:
        raise ReaderBusy from None
    try:
        yield
    finally:
        slots.release()
