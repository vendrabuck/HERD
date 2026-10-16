"""Sandbox isolation tests for the driver subprocess.

These cover the two hardening guarantees the execution service makes about the
driver child process:

1. Device credentials (password-typed context keys) are not copied into the
   child environment, so they cannot be read out of /proc/<pid>/environ. The
   driver still receives them via the context temp file.
2. POSIX resource limits (RLIMIT_AS, RLIMIT_CPU) are applied by the child
   wrapper (_runner.py) before it imports any driver code, so a driver that
   exhausts memory or CPU is killed rather than taking down the host. These
   limits are POSIX-only; the rlimit cases skip where os.fork is unavailable.
   The caps are applied in the child rather than via subprocess preexec_fn
   because preexec_fn is not thread-safe and driver execution runs under
   asyncio.to_thread (issue #307). These cases still spawn the real wrapper via
   execute_driver_method, so they prove the child actually enforces the limits.
3. No package file runs before those limits (issue #1180). The child starts in
   isolated mode (`python -I -B`), so neither a package `sitecustomize.py` nor
   a package module shadowing a standard library module the runner imports
   (`json`) runs at interpreter startup; the runner puts the package directory
   and `_deps` on sys.path itself, after the limits apply.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from app.services import driver_sandbox
from app.services.driver_sandbox import execute_driver_method

# RLIMIT_AS is honored on Linux but frequently ignored on macOS, so the
# memory-kill assertion is only reliable on a forking, non-Darwin platform.
_POSIX_FORK = hasattr(os, "fork") and sys.platform != "win32"
_skip_no_rlimit = pytest.mark.skipif(
    not _POSIX_FORK, reason="rlimit enforcement requires POSIX os.fork"
)


def _make_driver_dir(driver_code: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="herd_test_isolation_")
    with open(os.path.join(tmpdir, "driver.py"), "w") as f:
        f.write(driver_code)
    return tmpdir


# A driver that reports every os.environ key, so the test can assert no secret
# leaked into the child environment.
ENV_DUMP_DRIVER = """
import os

class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {
            "env_keys": sorted(os.environ.keys()),
            "has_transcript_path": "HERD_TRANSCRIPT_PATH" in os.environ,
            "context_password": self.context.get("HERD_password", "MISSING"),
        }
"""

# A driver whose login allocates far more memory than the AS limit under test.
MEMORY_HOG_DRIVER = """
class Driver:
    def __init__(self, context):
        pass

    def login(self):
        # Allocate ~200 MB; well past a small RLIMIT_AS.
        blob = bytearray(200 * 1024 * 1024)
        return {"allocated": len(blob)}
"""

# A driver whose login burns CPU forever; only an RLIMIT_CPU (SIGXCPU) should
# stop it, not the much-larger wall-clock timeout.
CPU_HOG_DRIVER = """
class Driver:
    def __init__(self, context):
        pass

    def login(self):
        x = 0
        while True:
            x += 1
        return {"never": x}
"""


def test_no_secret_key_leaks_into_env():
    """A driver dumping os.environ sees no key containing PASSWORD, and no
    HERD_-prefixed secret key, while the password is still present in context."""
    driver_dir = _make_driver_dir(ENV_DUMP_DRIVER)
    context = {
        "HERD_ip": "10.0.0.1",
        "HERD_password": "secret",
        "HERD_enable_secret": "topsecret",
    }
    result = execute_driver_method(
        driver_dir,
        "login",
        context,
        timeout=10,
        password_keys={"HERD_password", "HERD_enable_secret"},
    )
    assert result["success"] is True
    env_keys = result["output"]["env_keys"]
    assert not any("PASSWORD" in k for k in env_keys)
    assert "HERD_PASSWORD" not in env_keys
    assert "HERD_ENABLE_SECRET" not in env_keys
    # The transcript path env var the runner depends on is still present.
    assert result["output"]["has_transcript_path"] is True
    # The driver still receives the password via the context temp file.
    assert result["output"]["context_password"] == "secret"


@_skip_no_rlimit
def test_memory_limit_kills_driver(monkeypatch):
    """A driver exceeding RLIMIT_AS is killed; the run fails with the
    resource-limit signal message rather than succeeding."""
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_as_bytes", 64 * 1024 * 1024)
    # Keep the other limits out of the way for this case.
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 0)
    driver_dir = _make_driver_dir(MEMORY_HOG_DRIVER)
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    assert result["success"] is False
    # Either killed by a signal (SIGKILL/SIGSEGV) or a MemoryError surfaced;
    # in all cases the run must not report success.
    assert result["output"] is None


@_skip_no_rlimit
def test_cpu_limit_kills_driver(monkeypatch):
    """A CPU-bound driver is killed by RLIMIT_CPU (SIGXCPU) well before the much
    larger wall-clock timeout, proving the CPU limit (not the timeout) stopped
    it."""
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 1)
    # Do not let an address-space limit interfere with this case.
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_as_bytes", 0)
    driver_dir = _make_driver_dir(CPU_HOG_DRIVER)
    # Wall-clock timeout is generous (30s) so a failure here means CPU killed it,
    # not the timeout.
    result = execute_driver_method(driver_dir, "login", {}, timeout=30)
    assert result["success"] is False
    # SIGXCPU is signal 24; the error string surfaces the signal number.
    assert "signal" in (result["error"] or "").lower()


# --- issue #1180: no package file runs before the resource limits ---

# The address-space cap the probes below configure. Large enough for an
# ordinary interpreter, and distinct from the unlimited (-1, -1) a probe sees
# when it runs before the runner applies the caps.
_PROBE_AS_BYTES = 1024 * 1024 * 1024

# A probe module that records the RLIMIT_AS it sees at import time into the
# marker file named by HERD_PROBE_MARKER (the sandbox upper-cases the
# HERD_probe_marker context key into that environment variable).
_MARKER_PROBE = """
import os
import resource

_marker = os.environ.get("HERD_PROBE_MARKER")
if _marker:
    with open(_marker, "w") as _f:
        _f.write(repr(resource.getrlimit(resource.RLIMIT_AS)))
"""

STATUS_OK_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def status(self):
        return {"ok": True}
"""

JSON_USING_DRIVER = """
import json

class Driver:
    def __init__(self, context):
        self.context = context

    def status(self):
        return {"json_file": json.__file__, "dumped": json.dumps([1, 2])}
"""

SIBLING_PROBE_DRIVER = """
import herd_probe_sibling

class Driver:
    def __init__(self, context):
        self.context = context

    def status(self):
        return {"as_at_import": list(herd_probe_sibling.AS_AT_IMPORT)}
"""

SIBLING_PROBE_MODULE = """
import resource

AS_AT_IMPORT = resource.getrlimit(resource.RLIMIT_AS)
"""

# Reports how the child interpreter was started and what it resolves, from a
# package that also ships a decoy `driver_transcript.py`, a sibling module, and
# a module vendored under _deps/.
PATH_REPORT_DRIVER = """
import sys

import driver_transcript
import herd_probe_dep
import herd_probe_local

class Driver:
    def __init__(self, context):
        self.context = context

    def status(self):
        return {
            "isolated": sys.flags.isolated,
            "dont_write_bytecode": sys.flags.dont_write_bytecode,
            "path_head": sys.path[:3],
            "driver_transcript_file": driver_transcript.__file__,
            "local": herd_probe_local.WHERE,
            "dep": herd_probe_dep.WHERE,
        }
"""


def _limit_address_space_only(monkeypatch):
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_as_bytes", _PROBE_AS_BYTES)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 0)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nofile", 0)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nproc", 0)


@_skip_no_rlimit
def test_package_sitecustomize_never_runs_in_the_child(monkeypatch, tmp_path):
    """A package `sitecustomize.py` is never imported: with the package
    directory on PYTHONPATH it ran at interpreter startup, before the runner
    applied any limit (the review probe saw RLIMIT_AS (-1, -1)). Under `-I` the
    package directory is not on sys.path while `site` runs, so the module is
    never found, and the driver call itself still succeeds."""
    _limit_address_space_only(monkeypatch)
    driver_dir = _make_driver_dir(STATUS_OK_DRIVER)
    with open(os.path.join(driver_dir, "sitecustomize.py"), "w") as f:
        f.write(_MARKER_PROBE)
    marker = tmp_path / "sitecustomize.marker"

    result = execute_driver_method(
        driver_dir, "status", {"HERD_probe_marker": str(marker)}, timeout=10
    )

    assert not marker.exists(), f"sitecustomize ran, RLIMIT_AS seen: {marker.read_text()}"
    assert result["success"] is True, result
    assert result["output"] == {"ok": True}


@_skip_no_rlimit
def test_package_json_module_never_replaces_the_standard_library(monkeypatch, tmp_path):
    """A package `json.py` never runs: the runner imports the standard library
    `json` before the package directory is on sys.path, and sys.modules keeps
    serving that module, so the runner parses its context and prints its result
    with the real json, and the driver's own `import json` gets the standard
    library too. (With PYTHONPATH the shadow ran at the runner's import, before
    the limits, and broke the runner.)"""
    _limit_address_space_only(monkeypatch)
    driver_dir = _make_driver_dir(JSON_USING_DRIVER)
    with open(os.path.join(driver_dir, "json.py"), "w") as f:
        f.write(_MARKER_PROBE)
    marker = tmp_path / "json_shadow.marker"

    result = execute_driver_method(
        driver_dir, "status", {"HERD_probe_marker": str(marker)}, timeout=10
    )

    assert not marker.exists(), f"package json.py ran, RLIMIT_AS seen: {marker.read_text()}"
    assert result["success"] is True, result
    # The test process and the child run the same interpreter, so the standard
    # library json has the same file in both.
    assert result["output"] == {"json_file": json.__file__, "dumped": "[1, 2]"}


@_skip_no_rlimit
def test_package_module_the_driver_imports_runs_under_the_caps(monkeypatch):
    """Package code that does run (a sibling module driver.py imports) runs only
    after the runner applied the configured address-space cap."""
    _limit_address_space_only(monkeypatch)
    driver_dir = _make_driver_dir(SIBLING_PROBE_DRIVER)
    with open(os.path.join(driver_dir, "herd_probe_sibling.py"), "w") as f:
        f.write(SIBLING_PROBE_MODULE)

    result = execute_driver_method(driver_dir, "status", {}, timeout=10)

    assert result["success"] is True, result
    assert result["output"] == {"as_at_import": [_PROBE_AS_BYTES, _PROBE_AS_BYTES]}


def test_child_is_isolated_and_resolves_runner_then_package_then_deps(monkeypatch):
    """The child runs with `-I -B`, and the sys.path a driver sees starts with
    the runner directory, then the package directory, then _deps (the order
    PYTHONPATH used to give). So `driver_transcript` is the runner's sibling
    even when the package ships a file of that name, a sibling module and a
    vendored module both import, and no bytecode is written into the package."""
    _limit_address_space_only(monkeypatch)
    driver_dir = _make_driver_dir(PATH_REPORT_DRIVER)
    deps_dir = os.path.join(driver_dir, "_deps")
    os.makedirs(deps_dir)
    with open(os.path.join(driver_dir, "driver_transcript.py"), "w") as f:
        f.write('raise RuntimeError("package driver_transcript.py must not be imported")\n')
    with open(os.path.join(driver_dir, "herd_probe_local.py"), "w") as f:
        f.write('WHERE = "package"\n')
    with open(os.path.join(deps_dir, "herd_probe_dep.py"), "w") as f:
        f.write('WHERE = "_deps"\n')
    runner_dir = os.path.dirname(driver_sandbox._RUNNER_PATH)

    result = execute_driver_method(driver_dir, "status", {}, timeout=10)

    assert result["success"] is True, result
    assert result["output"] == {
        "isolated": 1,
        "dont_write_bytecode": True,
        "path_head": [runner_dir, driver_dir, deps_dir],
        "driver_transcript_file": os.path.join(runner_dir, "driver_transcript.py"),
        "local": "package",
        "dep": "_deps",
    }
    assert not os.path.exists(os.path.join(driver_dir, "__pycache__"))
    assert not os.path.exists(os.path.join(deps_dir, "__pycache__"))


def test_child_command_is_isolated_and_env_carries_no_pythonpath(monkeypatch):
    """The one child spawn is `python -I -B _runner.py ...`, and its env holds
    only PATH, the transcript path, and the rlimit policy (plus the context's
    non-secret keys, none here): no PYTHONPATH, even with a _deps directory, and
    no PYTHONDONTWRITEBYTECODE, both of which `-I` would ignore anyway."""
    driver_dir = _make_driver_dir(STATUS_OK_DRIVER)
    os.makedirs(os.path.join(driver_dir, "_deps"))
    seen = {}

    class _Done:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = ""

    def _capture_run(cmd, *a, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        return _Done()

    monkeypatch.setattr(driver_sandbox.subprocess, "run", _capture_run)
    result = execute_driver_method(driver_dir, "status", {}, timeout=10)

    assert result["success"] is True
    assert seen["cmd"][:6] == [
        "python",
        "-I",
        "-B",
        driver_sandbox._RUNNER_PATH,
        driver_dir,
        "status",
    ]
    assert len(seen["cmd"]) == 7  # plus the context file; no kwargs file
    assert set(seen["env"]) == {"PATH", "HERD_TRANSCRIPT_PATH", "HERD_SANDBOX_RLIMITS"}


def test_checked_in_mock_l1_driver_still_runs_in_the_isolated_child(tmp_path):
    """A real checked-in package still runs under `-I`: drivers/mock_l1, copied
    out of the tree, connects a port pair through the real sandbox, and its
    `from driver_transcript import record_command` reaches the runner's helper
    (the transcript row only exists if it did)."""
    repo_root = Path(__file__).resolve().parents[3]
    driver_dir = tmp_path / "mock_l1"
    shutil.copytree(repo_root / "drivers" / "mock_l1", driver_dir)

    result = execute_driver_method(
        str(driver_dir),
        "connect_ports",
        {"HERD_device_id": "d1"},
        method_kwargs={"port_a": "1/1", "port_b": "1/2"},
        timeout=10,
    )

    assert result["success"] is True, result
    assert result["output"] == {"success": True, "output": {"port_a": "1/1", "port_b": "1/2"}}
    assert [(row["command"], row["response"]) for row in result["transcript"]] == [
        ("connect_ports", "(ok)")
    ]
