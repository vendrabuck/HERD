"""Edge-branch coverage for driver_sandbox.py.

Covers the resource-limit policy (parent-side pair building from settings, the
child-side apply with its 0-means-unlimited skip and setrlimit failure swallow,
and the env var that carries the policy to the child), the pip-install exception
branch, the non-JSON-stdout-on-success fallback, and _read_transcript (missing
file, malformed line skip, OSError on read).
"""

import json
import os
import tempfile

import pytest
from app.services import _rlimits, driver_sandbox
from app.services.driver_sandbox import (
    _RLIMITS_ENV_KEY,
    _read_transcript,
    _rlimit_pairs,
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


# --- rlimit policy: parent pair-building, child apply, env transport ---


def test_rlimit_pairs_reads_settings_live(monkeypatch):
    """_rlimit_pairs maps each RLIMIT_* name to its live setting value."""
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_as_bytes", 111)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 222)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nofile", 333)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nproc", 444)
    assert _rlimit_pairs() == [
        ("RLIMIT_AS", 111),
        ("RLIMIT_CPU", 222),
        ("RLIMIT_NOFILE", 333),
        ("RLIMIT_NPROC", 444),
    ]


def test_apply_rlimits_applies_each_positive_limit(monkeypatch):
    """apply_rlimits calls setrlimit for each positive pair, resolving the name,
    and skips a 0 (unlimited) pair."""
    if not _rlimits.rlimits_supported():
        pytest.skip("non-POSIX platform")

    import resource

    calls = []

    def _fake_setrlimit(res, limits):
        calls.append((res, limits))

    monkeypatch.setattr(resource, "setrlimit", _fake_setrlimit)
    _rlimits.apply_rlimits([("RLIMIT_CPU", 5), ("RLIMIT_AS", 0), ("RLIMIT_NOFILE", 128)])
    # The 0 pair is skipped; the two positive limits are applied as soft==hard.
    assert calls == [
        (resource.RLIMIT_CPU, (5, 5)),
        (resource.RLIMIT_NOFILE, (128, 128)),
    ]


def test_apply_rlimits_skips_unknown_limit_name(monkeypatch):
    """An unrecognized RLIMIT name is skipped, not fatal."""
    if not _rlimits.rlimits_supported():
        pytest.skip("non-POSIX platform")

    import resource

    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda res, limits: calls.append(res))
    _rlimits.apply_rlimits([("RLIMIT_NOT_A_REAL_LIMIT", 5)])
    assert calls == []


def test_apply_rlimits_swallows_setrlimit_error(monkeypatch):
    """A setrlimit ValueError/OSError is swallowed, not raised (preexec parity)."""
    if not _rlimits.rlimits_supported():
        pytest.skip("non-POSIX platform")

    import resource

    def _boom(res, limits):
        raise OSError("cannot raise limit")

    monkeypatch.setattr(resource, "setrlimit", _boom)
    # Must not raise despite setrlimit failing for every limit.
    _rlimits.apply_rlimits([("RLIMIT_CPU", 5)])


def test_execute_passes_rlimit_policy_to_child_env(monkeypatch):
    """execute_driver_method hands the child the exact _rlimit_pairs policy via
    the env var, and a context key cannot clobber it."""
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_as_bytes", 777)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_cpu_seconds", 0)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nofile", 0)
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nproc", 0)

    driver_dir = _make_driver_dir(VALID_DRIVER)
    seen = {}

    class _Done:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = ""

    def _capture_run(cmd, *a, **kw):
        seen["env"] = kw["env"]
        # preexec_fn must no longer be used (thread-unsafe under to_thread).
        assert "preexec_fn" not in kw or kw["preexec_fn"] is None
        return _Done()

    monkeypatch.setattr(driver_sandbox.subprocess, "run", _capture_run)
    # A context key that uppercases onto the rlimits env key must not win.
    result = execute_driver_method(
        driver_dir, "login", {"HERD_sandbox_rlimits": "attacker"}, timeout=10
    )
    assert result["success"] is True
    assert seen["env"][_RLIMITS_ENV_KEY] == json.dumps(
        [["RLIMIT_AS", 777], ["RLIMIT_CPU", 0], ["RLIMIT_NOFILE", 0], ["RLIMIT_NPROC", 0]]
    )


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


# --- stderr no longer conflated with error on a clean exit (issue #394) ---


def test_success_with_stderr_carries_it_separately_and_leaves_error_none(monkeypatch):
    """A returncode-0 child with non-empty stderr (e.g. a netmiko deprecation
    warning) must not populate 'error': the text goes on the new 'stderr' key
    and 'error' stays None, so a SUCCESS run never carries a non-null error."""
    driver_dir = _make_driver_dir(VALID_DRIVER)

    class _Done:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = "DeprecationWarning: something benign\n"

    monkeypatch.setattr(driver_sandbox.subprocess, "run", lambda *a, **kw: _Done())
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    assert result["success"] is True
    assert result["error"] is None
    assert result["stderr"] == "DeprecationWarning: something benign\n"


def test_success_with_empty_stderr_leaves_both_error_and_stderr_none(monkeypatch):
    """A clean returncode-0 exit with no stderr output at all: 'error' and the
    new 'stderr' key are both None."""
    driver_dir = _make_driver_dir(VALID_DRIVER)

    class _Done:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = ""

    monkeypatch.setattr(driver_sandbox.subprocess, "run", lambda *a, **kw: _Done())
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    assert result["success"] is True
    assert result["error"] is None
    assert result["stderr"] is None


def test_failure_path_still_sources_error_from_stderr(monkeypatch):
    """Unchanged behavior: on a non-zero, non-signal returncode, 'error' is
    still populated from stderr (falling back to stdout, then a default)."""
    driver_dir = _make_driver_dir(VALID_DRIVER)

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "driver raised ValueError: bad config"

    monkeypatch.setattr(driver_sandbox.subprocess, "run", lambda *a, **kw: _Failed())
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    assert result["success"] is False
    assert result["error"] == "driver raised ValueError: bad config"


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
