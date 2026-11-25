"""Driver sandbox: execute driver methods in an isolated subprocess."""

import contextlib
import json
import logging
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_RUNNER_PATH = str(Path(__file__).parent / "_runner.py")

# The read size for a child's stdout and stderr, the one communicate() uses.
_READ_CHUNK_BYTES = 32768

# The name of the thread that waits for a sandbox child's exit (`_watch_exit`).
_WATCHER_THREAD_NAME = "herd-sandbox-exit-watch"


def _kill_group(pgid: int) -> None:
    """SIGKILL every process in the sandbox child's process group (issue #1407).

    `pgid` is the child's pid, which is also its process group id because the
    child starts in a session of its own (`_run_child`). Callers send it only
    while the child is not yet reaped, so the id still names the child's group.
    A group already gone is fine. The kill is refused for an id below 2 (0 would
    be this process's own group, 1 is init) and for this process's own group.
    That refusal is defense in depth: with the new session the id is the child's
    pid and never this process's group, and without one `killpg` of the child's
    pid finds no such group (ESRCH, suppressed here); it is what stands between a
    future change that looks the group up some other way and the service
    killing itself.
    """
    if pgid < 2 or pgid == os.getpgrp():
        logger.error("Refusing to signal process group %d: not a sandbox child's group", pgid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _watch_exit(pid: int, exit_w: int) -> None:
    """Close `exit_w` once the child `pid` has exited, without reaping it.

    Runs on a thread of its own. `os.waitid` with WNOWAIT returns when the child
    exits and leaves it a zombie, so its pid, and with it its group id, stays
    reserved until `_run_child` reaps it. A pidfd would do the same without a
    thread, but the python-build-standalone interpreters that uv installs (the
    3.12.12 and 3.14.0 builds were checked) have no `os.pidfd_open`.
    """
    try:
        with contextlib.suppress(ChildProcessError):
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    finally:
        os.close(exit_w)


def _collect(
    proc: subprocess.Popen, exit_r: int, pgid: int, deadline: float
) -> tuple[bytes, bytes] | None:
    """Read the child's stdout and stderr to their end and see it exit, by `deadline`.

    Returns (stdout, stderr) as bytes once the child has exited and both pipes
    are closed, or None when `deadline` (a `time.monotonic` value) passes first.
    The exit is seen as end of file on `exit_r`, whose write end `_watch_exit`
    closes when the child exits and before it is reaped. At that moment the
    child's group is killed, so a process the child started that holds a pipe
    open cannot keep the read waiting until the deadline; its pipe ends close
    when it dies. Nothing here reaps the child.
    """
    assert proc.stdout is not None and proc.stderr is not None
    out_fd = proc.stdout.fileno()
    err_fd = proc.stderr.fileno()
    chunks: dict[int, list[bytes]] = {out_fd: [], err_fd: []}
    with selectors.PollSelector() as selector:
        selector.register(out_fd, selectors.EVENT_READ)
        selector.register(err_fd, selectors.EVENT_READ)
        selector.register(exit_r, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            for key, _events in selector.select(remaining):
                if key.fd == exit_r:
                    selector.unregister(exit_r)
                    _kill_group(pgid)
                    continue
                data = os.read(key.fd, _READ_CHUNK_BYTES)
                if data:
                    chunks[key.fd].append(data)
                else:
                    selector.unregister(key.fd)
    return b"".join(chunks[out_fd]), b"".join(chunks[err_fd])


def _decode(data: bytes, stream) -> str:
    """Decode a pipe's bytes exactly as `subprocess.run(text=True)` does: the
    pipe's own encoding and error handler, then universal newlines."""
    return data.decode(stream.encoding, stream.errors).replace("\r\n", "\n").replace("\r", "\n")


def _run_child(
    cmd: list[str], *, env: dict[str, str], timeout: float, text: bool
) -> subprocess.CompletedProcess:
    """Run `cmd` with its stdout and stderr captured; nothing it started outlives it.

    The replacement for `subprocess.run(cmd, capture_output=True, timeout=...)`
    for the sandbox child and the `pip install` child (issue #1407): the same
    result (a CompletedProcess whose stdout and stderr are decoded as `text=True`
    decodes them, or bytes), the same `subprocess.TimeoutExpired` when the child
    has not exited, or its stdout or stderr is still open, `timeout` seconds after
    it started, and the same OSError when it cannot be started.

    The invariant: however the call ends (the child exits with any status or is
    killed by a signal, the timeout passes, or an exception is raised in this
    thread between the start and the reap), every process in the child's process
    group, the child included, is sent SIGKILL, and then the child is reaped.

    How it holds:
      - The child starts with `start_new_session=True`, so it leads a session and
        a process group of its own, whose id is its pid. The new session is made
        in the child before it executes the command, and Popen returns only after
        that, so no kill is ever sent before the group exists. The kill goes to
        that id only, never to 0 or to this process's group (`_kill_group`).
      - The group is killed only while the child is not yet reaped: when its exit
        is seen (`_collect`, told by `_watch_exit`, which waits for the exit
        without reaping), and in the `finally` below, before `wait()` reaps it.
        An unreaped child keeps its pid, and with it the group id, from being
        reused, so a kill can never reach a process group that a recycled id
        names, another sandbox call's included. The watcher thread is joined
        before the reap for the same reason: it never waits on a pid that is no
        longer the child's.
      - The kill at the exit is what lets a call whose driver left a helper
        holding the child's stdout or stderr end when the child does, rather
        than at the timeout.

    Assumed: nothing else in this process reaps the child. No code in the
    execution service or `herd_common` waits for any child but its own (no
    `waitpid(-1)`, no `os.wait`) or installs a SIGCHLD handler; a change that
    brought one in could reap the child behind this function, after which its
    pid, and the group id the kills use, could be reused.

    Not covered: a descendant that leaves the group (it calls setsid or setpgid,
    as a helper that daemonizes itself does) is not killed, and if it holds the
    child's stdout or stderr open, the call waits for it until the timeout and
    raises TimeoutExpired. A second exception raised in this thread while the
    `finally` is joining the watcher or waiting for the killed child leaves the
    child unreaped (it has been sent SIGKILL; Popen reaps it later) and leaves
    open the child's stdout and stderr pipes and the read end of the exit pipe.
    This runs on a worker thread (`asyncio.to_thread`), and cancelling the
    coroutine that awaits the thread does not stop it: the call still ends in
    one of the ways above.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=env,
        start_new_session=True,
    )
    # The child leads its own session, so its process group id is its pid.
    pgid = proc.pid
    exit_r = exit_w = None
    watcher = None
    try:
        deadline = time.monotonic() + timeout
        exit_r, exit_w = os.pipe()
        # Hand exit_w to the watcher before it starts: from here only the
        # watcher closes it, so no exception can make it closed twice.
        watcher_w, exit_w = exit_w, None
        thread = threading.Thread(
            target=_watch_exit,
            args=(proc.pid, watcher_w),
            name=_WATCHER_THREAD_NAME,
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            # The thread could not be started, so it never closes its end.
            os.close(watcher_w)
            raise
        watcher = thread
        collected = _collect(proc, exit_r, pgid, deadline)
        if collected is None:
            raise subprocess.TimeoutExpired(cmd, timeout)
        stdout, stderr = collected
        if text:
            stdout = _decode(stdout, proc.stdout)
            stderr = _decode(stderr, proc.stderr)
    finally:
        _kill_group(pgid)
        # The child itself, whatever became of the group kill: it is not reaped
        # yet, so its pid is still its own.
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGKILL)
        if watcher is not None:
            watcher.join()
        if exit_w is not None:
            os.close(exit_w)
        proc.wait()
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdout.close()
        proc.stderr.close()
        if exit_r is not None:
            os.close(exit_r)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def context_to_env_vars(context: dict, exclude: set[str] | None = None) -> dict[str, str]:
    """Convert HERD context dict to uppercase env-var strings.

    All keys are uppercased (e.g. HERD_ip becomes HERD_IP). Values are
    coerced to strings so they are valid OS environment variable values.

    Keys in `exclude` are dropped from the env copy (matched
    case-insensitively, since context keys are HERD_password but env keys are
    HERD_PASSWORD). The driver still receives those values via the context temp
    file; this only prevents secrets from being duplicated into the child
    environment, where they would be readable via /proc/<pid>/environ.
    """
    excluded_upper = {k.upper() for k in (exclude or set())}
    env_vars: dict[str, str] = {}
    for key, value in context.items():
        env_key = key.upper()
        if env_key in excluded_upper:
            continue
        if value is None:
            env_vars[env_key] = ""
        elif isinstance(value, bool):
            env_vars[env_key] = str(value).lower()
        elif isinstance(value, (int, float)):
            env_vars[env_key] = str(value)
        elif isinstance(value, str):
            env_vars[env_key] = value
        else:
            # list, dict, or other complex types: serialize as JSON
            env_vars[env_key] = json.dumps(value, default=str)
    return env_vars


# Env var carrying the rlimit policy from the parent to the child wrapper. The
# child (_runner.py) reads it and applies the caps BEFORE importing any driver
# code. See _rlimit_pairs and _rlimits.apply_rlimits.
_RLIMITS_ENV_KEY = "HERD_SANDBOX_RLIMITS"


def _rlimit_pairs() -> list[tuple[str, int]]:
    """Build the (RLIMIT_* name, value) pairs the child applies from live settings.

    Each pair maps a resource.RLIMIT_* attribute name to its configured cap;
    the child applies the value as both soft and hard limit. Read from settings
    live so tests can monkeypatch them; a value of 0 leaves that resource
    unlimited (the child skips it). This is the single source of which limits
    the sandbox enforces and their values: RLIMIT_AS (address space, prevents
    OOM-bomb drivers), RLIMIT_CPU (seconds, prevents infinite loops),
    RLIMIT_NOFILE (open files), RLIMIT_NPROC (processes, prevents fork-bombs).
    The child applies them generically, so the policy is not duplicated there.

    The caps are applied inside the child rather than via subprocess preexec_fn:
    CPython documents preexec_fn as not thread-safe (the child runs Python
    between fork and exec and can deadlock on a lock, e.g. the allocator or
    import lock, held by another thread at fork time). Since driver execution
    runs under asyncio.to_thread, multiple worker threads fork concurrently, so
    a preexec_fn hook is unsafe; the child applies the identical caps itself (#307).
    """
    return [
        ("RLIMIT_AS", settings.driver_rlimit_as_bytes),
        ("RLIMIT_CPU", settings.driver_rlimit_cpu_seconds),
        ("RLIMIT_NOFILE", settings.driver_rlimit_nofile),
        ("RLIMIT_NPROC", settings.driver_rlimit_nproc),
    ]


class DryRunRefused(RuntimeError):
    """Raised when a dry-run is requested against a driver that did not opt in.

    The sandbox refuses to spawn the subprocess so an unmodified driver
    cannot ignore context["dry_run"] and hit the wire. Inventory's schedule
    endpoint also gates this at the upstream layer; this is defense in depth.
    """


def execute_driver_method(
    driver_path: str,
    action: str,
    context: dict,
    port_a: str | None = None,
    port_b: str | None = None,
    method_kwargs: dict | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
    driver_metadata: dict | None = None,
    password_keys: set[str] | None = None,
) -> dict:
    """Execute a driver method in a sandboxed subprocess.

    Args:
        driver_path: path to the extracted driver directory
        action: method name to call (e.g. "login", "connect_ports", "add_to_vlan")
        context: the context dict to pass to Driver.__init__()
        port_a: first port name (for L1 connect_ports/disconnect_ports; legacy compat)
        port_b: second port name (for L1 connect_ports/disconnect_ports; legacy compat)
        method_kwargs: keyword arguments to pass to the driver method. If port_a/port_b
            are also provided, they are merged in (method_kwargs takes precedence).
        timeout: execution timeout in seconds
        dry_run: when True, the driver is expected to honor context["dry_run"]
            and skip wire I/O. Refuses (DryRunRefused) when the driver's
            metadata does not declare supports_dry_run=True; this prevents an
            unmodified driver from silently pushing the config for real.
        driver_metadata: the driver's parsed driver_metadata.json (or
            DEFAULT_DRIVER_METADATA when absent). Required when dry_run=True;
            ignored otherwise.
        password_keys: HERD_-prefixed context keys that hold secrets (from the
            template's password-typed fields). These are stripped from the child
            environment so credentials are not duplicated into /proc/<pid>/environ;
            the driver still receives them via the context temp file.

    Returns:
        dict with keys: success (bool), output (dict or None), error (str or None),
        duration_ms (int), transcript (list[dict]). Transcript rows come from
        the driver's `record_command` calls; runs whose drivers do not use the
        helper return an empty list. When the driver call (or its
        instantiation, or driver loading inside the child) RAISED (issue
        #840), two extra keys are present: exception_class (str, e.g.
        "AttributeError") and exception_message (str, the raw exception
        text). `error` is then already the safe, class-name-only string
        (f"driver raised {exception_class}"); exception_message is for the
        caller to LOG ONLY, since it can carry hosts, paths, or
        credential-adjacent text, and must never be stored on a row or
        returned to an API caller.

    Raises:
        Nothing; errors are captured in the return dict.
    """
    if timeout is None:
        if action == "status":
            timeout = settings.status_check_timeout_seconds
        else:
            timeout = settings.execution_timeout_seconds

    # Dry-run safety: refuse to spawn if the driver did not opt in. An
    # unmodified driver that ignores context["dry_run"] would push the config
    # for real, which is exactly what a dry-run is supposed to prevent.
    if dry_run:
        meta = driver_metadata or {}
        if not bool(meta.get("supports_dry_run", False)):
            raise DryRunRefused(
                "driver does not advertise dry-run support; "
                "refuse to fire a dry-run that would hit the wire"
            )

    # Inject dry_run into context using the unprefixed key the driver contract
    # specifies. Use a shallow copy so callers' dicts are not mutated.
    if dry_run:
        context = dict(context)
        context["dry_run"] = True

    # Write context to a temp file (not CLI args, to avoid exposing passwords in process list)
    context_file = None
    kwargs_file = None
    transcript_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="herd_ctx_"
        ) as f:
            json.dump(context, f)
            context_file = f.name

        # Empty file the subprocess will append JSONL rows to via driver_transcript.record_command.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, prefix="herd_tx_"
        ) as f:
            transcript_file = f.name

        # Isolated mode (issue #1180). `-I` keeps every package file out of the
        # interpreter's startup: no PYTHONPATH (it implies `-E`, so every
        # PYTHON* variable is ignored), no user site directory, and (since 3.11,
        # it implies `-P`) no script directory on sys.path. Without it the
        # package directory was on sys.path from the first instruction, so a
        # package `sitecustomize.py` ran at startup and a package `json.py`
        # replaced the standard library for the runner's own `import json`,
        # both before _runner.py applied the resource limits. The runner puts
        # the package directory (and `_deps`) on sys.path itself, after the
        # limits. `-B` replaces PYTHONDONTWRITEBYTECODE, which `-I` ignores.
        cmd = ["python", "-I", "-B", _RUNNER_PATH, driver_path, action, context_file]
        # Merge port_a/port_b into method_kwargs for backward compatibility
        effective_kwargs = dict(method_kwargs or {})
        if port_a is not None and "port_a" not in effective_kwargs:
            effective_kwargs["port_a"] = port_a
        if port_b is not None and "port_b" not in effective_kwargs:
            effective_kwargs["port_b"] = port_b
        if effective_kwargs:
            # The keyword arguments travel in their own temp file, like the
            # context and for the same reason: a `configure` call's arguments
            # are the device configuration, and the child's command line is
            # readable by any process in the container (/proc/<pid>/cmdline,
            # ps). Only the file's path is on argv. Same lifecycle and
            # permissions as the context file (mode 0600, deleted below).
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="herd_kw_"
            ) as f:
                json.dump(effective_kwargs, f)
                kwargs_file = f.name
            cmd.append(kwargs_file)

        # No PYTHONPATH or PYTHONDONTWRITEBYTECODE: the child runs with `-I`,
        # which ignores both, so setting them would only mislead a reader.
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HERD_TRANSCRIPT_PATH": transcript_file,
        }
        # Inject HERD_ context values as env vars for debugging/observability,
        # but strip password-typed keys so device credentials are not duplicated
        # into the child environment (readable via /proc/<pid>/environ). The
        # driver still receives every value, secrets included, via the context
        # temp file written above.
        env.update(context_to_env_vars(context, exclude=password_keys or set()))

        # Hand the rlimit policy to the child wrapper, which applies it before
        # importing any driver code. Set after the context env update so a
        # context key can never clobber it.
        env[_RLIMITS_ENV_KEY] = json.dumps(_rlimit_pairs())

        # Install a driver's requirements.txt only when explicitly enabled. A
        # runtime `pip install` fetches arbitrary code from the network as the
        # service uid, so it is gated behind allow_driver_pip_install (default
        # off). When off, drivers must vendor their deps into _deps/. The
        # install subprocess deliberately does NOT apply the driver rlimits: it
        # does not run the _runner.py wrapper (which reads _RLIMITS_ENV_KEY), so
        # the 256MB RLIMIT_AS never bites pip. It runs as `python -I -B -m pip`
        # for the same reasons the child runs with those flags (issue #1180):
        # pip must not import a `sitecustomize.py` or a stdlib-shadowing module
        # from the package directory or the current directory, and `-B` stands
        # in for the PYTHONDONTWRITEBYTECODE that `-I` ignores. It runs through
        # `_run_child`, like the driver child, so nothing pip starts outlives it
        # (issue #1407).
        deps_dir = os.path.join(driver_path, "_deps")
        reqs_file = os.path.join(driver_path, "requirements.txt")
        if os.path.exists(reqs_file) and not os.path.exists(deps_dir):
            if settings.allow_driver_pip_install:
                try:
                    _run_child(
                        [
                            "python",
                            "-I",
                            "-B",
                            "-m",
                            "pip",
                            "install",
                            "-r",
                            reqs_file,
                            "--target",
                            deps_dir,
                            "-q",
                        ],
                        env=env,
                        timeout=60,
                        text=False,
                    )
                except Exception as e:
                    logger.warning("Failed to install driver dependencies: %s", e)
            else:
                logger.info(
                    "Driver has a requirements.txt but allow_driver_pip_install is off; "
                    "skipping install. Vendor deps into _deps/ or enable the flag."
                )

        # The child finds `_deps` itself (_runner._put_package_on_path, from
        # driver_path and the same existence test), after the resource limits.

        start = time.monotonic()
        try:
            # The child and every process it starts that stays in its process
            # group end with the call, however it ends (issue #1407, _run_child).
            result = _run_child(cmd, env=env, timeout=timeout, text=True)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            transcript = _read_transcript(transcript_file)
            if result.returncode == 0:
                try:
                    output = json.loads(result.stdout)
                except json.JSONDecodeError:
                    output = {"raw_output": result.stdout}
                return {
                    "success": True,
                    "output": output,
                    "error": None,
                    "stderr": result.stderr if result.stderr else None,
                    "duration_ms": elapsed_ms,
                    "transcript": transcript,
                }
            else:
                # A negative returncode means the child was killed by a signal,
                # e.g. SIGKILL when RLIMIT_AS is exceeded or SIGXCPU when
                # RLIMIT_CPU is exceeded. stderr is usually empty in that case,
                # so surface the signal instead of a bare "Unknown error".
                if result.returncode < 0:
                    error = (
                        f"driver killed by signal {-result.returncode} (likely a resource limit)"
                    )
                    return {
                        "success": False,
                        "output": None,
                        "error": error,
                        "duration_ms": elapsed_ms,
                        "transcript": transcript,
                    }
                # A positive returncode means _runner.py's own except-and-exit-1
                # handler ran, which prints structured JSON (issue #840): a
                # driver method (or its instantiation, or driver loading inside
                # the child) raised. Surface only the exception CLASS as
                # 'error' (what a caller may store or return); the full
                # exception_message rides along for the caller to LOG, never to
                # persist or return, since it can carry hosts, paths, or
                # credential-adjacent text. A stderr that does not parse as
                # that JSON shape (an older _runner.py, or a failure that never
                # reached its handler) falls back to the old plain-text
                # behavior unchanged.
                parsed_exception = _parse_driver_exception(result.stderr)
                if parsed_exception is not None:
                    exception_class, exception_message = parsed_exception
                    return {
                        "success": False,
                        "output": None,
                        "error": f"driver raised {exception_class}",
                        "exception_class": exception_class,
                        "exception_message": exception_message,
                        "duration_ms": elapsed_ms,
                        "transcript": transcript,
                    }
                # No structured line at all: an older _runner.py, or a child
                # that died before its handler ran. The raw stderr is NOT
                # stored as 'error' (issue #840): a caller may persist or return
                # 'error', and unstructured child output can carry the same
                # hosts, paths, and credential-adjacent text an exception
                # message can. It rides on the separate 'stderr' key, exactly
                # as it does on a clean exit (issue #394), for callers to LOG.
                return {
                    "success": False,
                    "output": None,
                    "error": f"driver process exited with status {result.returncode}",
                    "stderr": result.stderr or result.stdout or None,
                    "duration_ms": elapsed_ms,
                    "transcript": transcript,
                }
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return {
                "success": False,
                "output": None,
                "error": f"Execution timed out after {timeout}s",
                "duration_ms": elapsed_ms,
                "transcript": _read_transcript(transcript_file),
            }
    finally:
        if context_file and os.path.exists(context_file):
            os.unlink(context_file)
        if kwargs_file and os.path.exists(kwargs_file):
            os.unlink(kwargs_file)
        if transcript_file and os.path.exists(transcript_file):
            os.unlink(transcript_file)


def extract_config_schema(driver_path: str, timeout: int | None = None) -> dict:
    """Extract a driver's published config schema via the sandbox.

    Invokes the `__config_schema__` sentinel action, which reads
    Driver.config_schema() on the class object without instantiation (so a
    credential-dependent __init__ never runs). The classmethod is still
    untrusted code, so it runs under the same rlimit subprocess and the short
    status timeout, preventing credential leaks even during schema inspection.
    At driver load time execution caches the schema per-SHA256; mismatches
    between driver versions are naturally scoped. Returns the standard
    execute_driver_method result dict; on success `output` is
    {"has_schema": bool, "schema": dict | None}. Callers must treat
    non-dict schema, failed run, or timeout as "no usable schema" and fall
    back to the in-process registry; this helper never raises so extraction
    failures do not block driver load.
    """
    if timeout is None:
        timeout = settings.status_check_timeout_seconds
    return execute_driver_method(
        driver_path,
        action="__config_schema__",
        context={},
        timeout=timeout,
    )


def _parse_driver_exception(stderr: str | None) -> tuple[str, str] | None:
    """Parse _runner.py's structured "driver raised" stderr line (issue #840).

    On any exception escaping main(), _runner.py prints a single JSON line
    {"exception_class": ..., "message": ...} to stderr instead of plain
    text. Returns (exception_class, message) on a match; returns None for
    anything else (empty stderr, non-JSON text, JSON that is not that exact
    shape), which tells the caller to fall back to treating stderr as plain
    text, matching the pre-#840 behavior.

    Only the LAST non-empty line is parsed. The handler's line is the last
    thing the child writes before it exits, but it is rarely the only thing on
    stderr: a Python warning, a paramiko or netmiko log record, or a driver's
    own print all land ahead of it. Parsing the whole stream would fail on any
    of those and drop the caller into the unstructured branch, which is worst
    exactly when a network driver cannot reach its device: the noisy case and
    the case whose message names a host.
    """
    if not stderr or not stderr.strip():
        return None
    last_line = stderr.strip().splitlines()[-1].strip()
    try:
        parsed = json.loads(last_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    exception_class = parsed.get("exception_class")
    message = parsed.get("message")
    if not isinstance(exception_class, str) or not isinstance(message, str):
        return None
    return exception_class, message


def _read_transcript(path: str | None) -> list[dict]:
    """Parse the transcript JSONL file the subprocess wrote to.

    Each line is one command-response row from `driver_transcript.record_command`.
    Malformed lines are skipped with a warning; the transcript is observational
    and must not fail a run.
    """
    if not path or not os.path.exists(path):
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed transcript line: %r", line[:200])
    except OSError as exc:
        logger.warning("Failed to read transcript file %s: %s", path, exc)
    return rows
