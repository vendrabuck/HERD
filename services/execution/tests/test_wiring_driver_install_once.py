"""The driver dependency install runs once per wiring driver action (issue #1495).

With ALLOW_DRIVER_PIP_INSTALL on, a package that has a `requirements.txt` and no `_deps`
used to get a `pip install` inside `execute_driver_method` on EVERY sandbox attempt, each
with its own 60 second timeout and outside every timer the wiring claim budget counts.
An install that failed or timed out left no `_deps`, so all three attempts of an action
paid it again. The owner chose option (b): install once per action, before the first
attempt, and let the attempts run without it.

These tests drive `nats_consumer._run_driver_with_retry` through the REAL
`execute_driver_method` and real driver children; only the pip command is stubbed (no
network install), through `driver_sandbox._run_child`. The budget side, and the virtual
clock drive that shows a claim still held at the end of a drive with the install in
front of every action, are in `test_wiring_claim.py`.
"""

import os
import subprocess
import tempfile
from unittest.mock import patch

import pytest
from app.services import driver_sandbox
from app.services.nats_consumer import WIRING_DRIVER_ATTEMPTS, _run_driver_with_retry

pytestmark = pytest.mark.timeout(60)

PIP = ["python", "-I", "-B", "-m", "pip", "install"]

PASSING_DRIVER = """
class Driver:
    def __init__(self, context):
        pass

    def login(self):
        return {"success": True}
"""

FAILING_DRIVER = """
class Driver:
    def __init__(self, context):
        pass

    def login(self):
        raise RuntimeError("Connection refused")
"""


def _package(driver_code: str, *, requirements: bool = True) -> str:
    driver_dir = tempfile.mkdtemp(prefix="herd_test_install_once_")
    with open(os.path.join(driver_dir, "driver.py"), "w") as f:
        f.write(driver_code)
    if requirements:
        with open(os.path.join(driver_dir, "requirements.txt"), "w") as f:
            f.write("# empty requirements\n")
    return driver_dir


def _spy(monkeypatch, pip_behaviour):
    """Record every `_run_child` call in order as "pip" or "child"; the pip command is
    answered by `pip_behaviour(cmd)`, the driver child runs for real."""
    real_run = driver_sandbox._run_child
    order: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:6] == PIP:
            order.append("pip")
            return pip_behaviour(cmd)
        order.append("child")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(driver_sandbox, "_run_child", fake_run)
    return order


def _times_out(cmd):
    raise subprocess.TimeoutExpired(cmd, driver_sandbox.DRIVER_PIP_INSTALL_TIMEOUT_SECONDS)


def _exits_non_zero(cmd):
    return subprocess.CompletedProcess(cmd, 1, b"", b"")


def _raises(cmd):
    raise OSError("cannot start pip")


def _installs(cmd):
    # What a successful pip run leaves: the --target directory.
    os.makedirs(cmd[cmd.index("--target") + 1])
    return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture(autouse=True)
def _no_backoff_wait():
    """The in-line backoff sleeps are not what is under test; keep them instant. Only
    `nats_consumer` sees the instant sleep (its `asyncio` name is swapped for a proxy),
    so the event loop's own `asyncio` is untouched."""
    import asyncio

    class _AsyncioWithInstantSleep:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay, *args, **kwargs):
            await asyncio.sleep(0)

    with patch("app.services.nats_consumer.asyncio", _AsyncioWithInstantSleep()):
        yield


@pytest.mark.parametrize("pip_behaviour", [_times_out, _exits_non_zero, _raises])
async def test_a_failed_install_is_tried_once_per_action_not_once_per_attempt(
    monkeypatch, pip_behaviour
):
    """The reproduction: a failing install in front of a driver whose every attempt
    fails. The install runs once, before the first attempt, and the three attempts run
    the driver child with no install between them (before #1495: pip, child, three
    times)."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    order = _spy(monkeypatch, pip_behaviour)

    ok, attempts, err, _result = await _run_driver_with_retry(
        _package(FAILING_DRIVER), "login", {}, set()
    )

    assert (ok, attempts, err) == (False, WIRING_DRIVER_ATTEMPTS, "driver raised RuntimeError")
    assert order == ["pip"] + ["child"] * WIRING_DRIVER_ATTEMPTS


async def test_each_action_tries_the_install_once_again():
    """Once per ACTION: the next action of the drive (the next call) tries an install
    that is still due once more, so a fixed index is picked up by the next action."""
    # Its own monkeypatch scope, so the two calls share one spy.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
        order = _spy(mp, _times_out)
        package = _package(FAILING_DRIVER)
        await _run_driver_with_retry(package, "login", {}, set())
        await _run_driver_with_retry(package, "login", {}, set())
    assert order == (["pip"] + ["child"] * WIRING_DRIVER_ATTEMPTS) * 2


async def test_an_install_that_succeeds_is_not_repeated_by_the_next_action(monkeypatch):
    """A successful install leaves `_deps`, so the next action has nothing to install."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    order = _spy(monkeypatch, _installs)
    package = _package(PASSING_DRIVER)

    first = await _run_driver_with_retry(package, "login", {}, set())
    second = await _run_driver_with_retry(package, "login", {}, set())

    assert first[:3] == (True, 1, None)
    assert second[:3] == (True, 1, None)
    assert order == ["pip", "child", "child"]


@pytest.mark.parametrize(("flag", "requirements"), [(False, True), (True, False)])
async def test_no_install_runs_when_none_is_due_or_enabled(monkeypatch, flag, requirements):
    """With the flag off, or a package without a requirements.txt, no pip runs at all;
    the attempts are unchanged."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", flag)
    order = _spy(monkeypatch, lambda cmd: pytest.fail("pip must not run"))

    ok, attempts, _err, _result = await _run_driver_with_retry(
        _package(FAILING_DRIVER, requirements=requirements), "login", {}, set()
    )

    assert (ok, attempts) == (False, WIRING_DRIVER_ATTEMPTS)
    assert order == ["child"] * WIRING_DRIVER_ATTEMPTS


async def test_every_attempt_is_run_without_the_sandbox_install():
    """The seam itself: each attempt reaches the sandbox with `install_dependencies`
    False, after exactly one `_prepare_driver_dependencies` call."""
    calls: list[tuple] = []

    async def _prepare(driver_path):
        calls.append(("prepare", driver_path))
        return driver_sandbox.DEPS_INSTALL_FAILED

    async def _sandbox(driver_path, action, context, **kwargs):
        calls.append(("attempt", kwargs.get("install_dependencies")))
        return {"success": False, "output": None, "error": "boom", "duration_ms": 1}

    with (
        patch("app.services.nats_consumer._prepare_driver_dependencies", _prepare),
        patch("app.services.nats_consumer._run_sandbox", _sandbox),
    ):
        ok, attempts, err, _result = await _run_driver_with_retry("/pkg", "login", {}, set())

    assert (ok, attempts, err) == (False, WIRING_DRIVER_ATTEMPTS, "boom")
    assert calls == [("prepare", "/pkg")] + [("attempt", False)] * WIRING_DRIVER_ATTEMPTS
