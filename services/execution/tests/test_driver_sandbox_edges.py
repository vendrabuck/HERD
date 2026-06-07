"""Edge-branch coverage for driver_sandbox.py.

Covers the resource-limit preexec_fn (POSIX apply + setrlimit failure swallow,
and the non-POSIX None return), the pip-install exception branch, the
non-JSON-stdout-on-success fallback, and _read_transcript (missing file,
malformed line skip, OSError on read).
"""

import json
import os
import tempfile

import pytest
from app.services import driver_sandbox
from app.services.driver_sandbox import (
    _build_preexec_fn,
    _read_transcript,
    execute_driver_method,
)


def _make_driver_dir(driver_code: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="herd_test_driver_")
    with open(os.path.join(tmpdir, "driver.py"), "w") as f:
        f.write(driver_code)
    return tmpdir


VALID_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self):
        return {"success": True}
"""


# --- _build_preexec_fn ---


def test_build_preexec_fn_returns_none_on_win32(monkeypatch):
    monkeypatch.setattr(driver_sandbox.sys, "platform", "win32")
    assert _build_preexec_fn() is None


def test_build_preexec_fn_applies_rlimits(monkeypatch):
    """On POSIX, the returned callable calls setrlimit for each positive limit."""
    if not hasattr(os, "fork"):
        pytest.skip("non-POSIX platform")

    calls = []
    import resource

    def _fake_setrlimit(res, limits):
        calls.append((res, limits))

    monkeypatch.setattr(resource, "setrlimit", _fake_setrlimit)
    # Ensure at least one positive limit is configured.
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 5)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_as_bytes", 0)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nofile", 0)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nproc", 0)

    apply_fn = _build_preexec_fn()
    assert apply_fn is not None
    apply_fn()
    # Only the one positive limit (CPU) was applied.
    assert calls == [(resource.RLIMIT_CPU, (5, 5))]


def test_build_preexec_fn_swallows_setrlimit_error(monkeypatch):
    """A setrlimit ValueError/OSError inside the child is swallowed, not raised."""
    if not hasattr(os, "fork"):
        pytest.skip("non-POSIX platform")

    import resource

    def _boom(res, limits):
        raise OSError("cannot raise limit")

    monkeypatch.setattr(resource, "setrlimit", _boom)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 5)

    apply_fn = _build_preexec_fn()
    # Must not raise despite setrlimit failing for every limit.
    apply_fn()


# --- pip-install exception branch (lines 216-217) ---


def test_pip_install_exception_swallowed(monkeypatch):
    """If the pip install subprocess raises, it is logged and the driver still runs."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    driver_dir = _make_driver_dir(VALID_DRIVER)
    with open(os.path.join(driver_dir, "requirements.txt"), "w") as f:
        f.write("# empty\n")

    real_run = driver_sandbox.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["pip", "install"]:
            raise RuntimeError("pip exploded")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(driver_sandbox.subprocess, "run", fake_run)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "x"}, timeout=15)
    # The pip failure did not abort the driver run.
    assert result["success"] is True


# --- non-JSON stdout on returncode 0 (lines 243-244) ---


def test_success_with_non_json_stdout_wraps_raw_output(monkeypatch):
    """A returncode-0 child whose stdout is not JSON yields {'raw_output': ...}."""
    driver_dir = _make_driver_dir(VALID_DRIVER)

    class _Done:
        returncode = 0
        stdout = "this is not json"
        stderr = ""

    monkeypatch.setattr(driver_sandbox.subprocess, "run", lambda *a, **kw: _Done())
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    assert result["success"] is True
    assert result["output"] == {"raw_output": "this is not json"}


# --- dry-run gate (lines 147-149, 157-158) ---


def test_dry_run_refused_without_support():
    from app.services.driver_sandbox import DryRunRefused

    driver_dir = _make_driver_dir(VALID_DRIVER)
    with pytest.raises(DryRunRefused):
        execute_driver_method(
            driver_dir,
            "login",
            {},
            dry_run=True,
            driver_metadata={"supports_dry_run": False},
            timeout=10,
        )


def test_dry_run_injects_context_flag(monkeypatch):
    """With supports_dry_run, dry_run=True injects context['dry_run']=True for the child."""
    driver_dir = _make_driver_dir(VALID_DRIVER)
    seen = {}

    class _Done:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = ""

    def _capture_run(cmd, *a, **kw):
        # cmd is [python, _runner.py, driver_path, action, context_file, ...].
        ctx_path = cmd[4]
        with open(ctx_path) as f:
            seen["ctx"] = json.load(f)
        return _Done()

    monkeypatch.setattr(driver_sandbox.subprocess, "run", _capture_run)
    result = execute_driver_method(
        driver_dir,
        "login",
        {"HERD_device_id": "x"},
        dry_run=True,
        driver_metadata={"supports_dry_run": True},
        timeout=10,
    )
    assert result["success"] is True
    assert seen["ctx"]["dry_run"] is True


# --- signal-kill error message (line 258) ---


def test_negative_returncode_reports_signal(monkeypatch):
    """A child killed by a signal (negative returncode) surfaces the signal number."""
    driver_dir = _make_driver_dir(VALID_DRIVER)

    class _Killed:
        returncode = -9  # SIGKILL, e.g. RLIMIT_AS exceeded
        stdout = ""
        stderr = ""

    monkeypatch.setattr(driver_sandbox.subprocess, "run", lambda *a, **kw: _Killed())
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    assert result["success"] is False
    assert "signal 9" in result["error"]
    assert "resource limit" in result["error"]


# --- _read_transcript ---


def test_read_transcript_missing_path_returns_empty():
    assert _read_transcript(None) == []
    assert _read_transcript("/nonexistent/herd_tx_missing.jsonl") == []


def test_read_transcript_parses_and_skips_malformed():
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, prefix="herd_tx_"
    ) as f:
        f.write(json.dumps({"command": "a"}) + "\n")
        f.write("\n")  # blank line skipped
        f.write("{not valid json}\n")  # malformed line skipped
        f.write(json.dumps({"command": "b"}) + "\n")
        path = f.name
    try:
        rows = _read_transcript(path)
        assert [r["command"] for r in rows] == ["a", "b"]
    finally:
        os.unlink(path)


def test_read_transcript_oserror_returns_empty(monkeypatch):
    """If opening the transcript raises OSError, return [] (observational, never fatal)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, prefix="herd_tx_"
    ) as f:
        path = f.name

    real_open = open

    def _boom(p, *a, **kw):
        if p == path:
            raise OSError("read error")
        return real_open(p, *a, **kw)

    monkeypatch.setattr("builtins.open", _boom)
    try:
        assert _read_transcript(path) == []
    finally:
        os.unlink(path)
