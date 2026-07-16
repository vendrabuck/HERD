"""Driver sandbox: execute driver methods in an isolated subprocess."""

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_RUNNER_PATH = str(Path(__file__).parent / "_runner.py")


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
        helper return an empty list.

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

        cmd = ["python", _RUNNER_PATH, driver_path, action, context_file]
        # Merge port_a/port_b into method_kwargs for backward compatibility
        effective_kwargs = dict(method_kwargs or {})
        if port_a is not None and "port_a" not in effective_kwargs:
            effective_kwargs["port_a"] = port_a
        if port_b is not None and "port_b" not in effective_kwargs:
            effective_kwargs["port_b"] = port_b
        if effective_kwargs:
            cmd.append(json.dumps(effective_kwargs))

        env = {
            "PYTHONPATH": driver_path,
            "PYTHONDONTWRITEBYTECODE": "1",
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
        # the 256MB RLIMIT_AS never bites pip.
        deps_dir = os.path.join(driver_path, "_deps")
        reqs_file = os.path.join(driver_path, "requirements.txt")
        if os.path.exists(reqs_file) and not os.path.exists(deps_dir):
            if settings.allow_driver_pip_install:
                try:
                    subprocess.run(
                        ["pip", "install", "-r", reqs_file, "--target", deps_dir, "-q"],
                        timeout=60,
                        capture_output=True,
                        env=env,
                    )
                except Exception as e:
                    logger.warning("Failed to install driver dependencies: %s", e)
            else:
                logger.info(
                    "Driver has a requirements.txt but allow_driver_pip_install is off; "
                    "skipping install. Vendor deps into _deps/ or enable the flag."
                )

        if os.path.exists(deps_dir):
            env["PYTHONPATH"] = f"{driver_path}:{deps_dir}"

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
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
                    "error": result.stderr if result.stderr else None,
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
                else:
                    error = result.stderr or result.stdout or "Unknown error"
                return {
                    "success": False,
                    "output": None,
                    "error": error,
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
