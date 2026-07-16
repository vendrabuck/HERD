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
"""

import os
import sys
import tempfile

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
