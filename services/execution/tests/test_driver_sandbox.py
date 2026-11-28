import asyncio
import contextlib
import functools
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.services import driver_sandbox
from app.services.driver_sandbox import context_to_env_vars, execute_driver_method

# Every test here runs real children; a defect that leaves the parent waiting on a
# child (a kill dropped, a wait with no deadline) fails within this bound instead
# of hanging until a driver's own two-minute sleep ends. The slowest passing test
# takes about 5 seconds.
pytestmark = pytest.mark.timeout(60)


def _make_driver_dir(driver_code: str) -> str:
    """Create a temp directory with driver.py and return the path."""
    tmpdir = tempfile.mkdtemp(prefix="herd_test_driver_")
    with open(os.path.join(tmpdir, "driver.py"), "w") as f:
        f.write(driver_code)
    return tmpdir


VALID_L1_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True, "port_a": port_a, "port_b": port_b}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True, "port_a": port_a, "port_b": port_b}

    def status(self):
        return {"reachable": True, "uptime": 1234}
"""

FAILING_DRIVER = """
class Driver:
    def __init__(self, context):
        pass

    def login(self):
        raise RuntimeError("Connection refused")

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        return {"reachable": False}
"""

SLOW_DRIVER = """
import time

class Driver:
    def __init__(self, context):
        pass

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        time.sleep(30)
        return {"reachable": True}
"""

CONTEXT_READER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        return {
            "reachable": True,
            "device_id": self.context.get("HERD_device_id"),
            "ip": self.context.get("HERD_ip_address"),
        }
"""


def test_execute_login():
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=10)
    assert result["success"] is True
    assert result["output"]["success"] is True
    assert result["duration_ms"] >= 0


def test_execute_connect_ports():
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(
        driver_dir,
        "connect_ports",
        {"HERD_device_id": "test"},
        port_a="1/1/1",
        port_b="1/1/2",
        timeout=10,
    )
    assert result["success"] is True
    assert result["output"]["port_a"] == "1/1/1"
    assert result["output"]["port_b"] == "1/1/2"


def test_execute_status():
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(driver_dir, "status", {"HERD_device_id": "test"}, timeout=10)
    assert result["success"] is True
    assert result["output"]["reachable"] is True
    assert result["output"]["uptime"] == 1234


def test_execute_failing_driver():
    """A driver method that raises (issue #840): 'error' is class-name-only,
    never the raw message (which can carry secret-adjacent text), and the raw
    message survives only in the new exception_message field for the caller
    to log."""
    driver_dir = _make_driver_dir(FAILING_DRIVER)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=10)
    assert result["success"] is False
    assert result["error"] == "driver raised RuntimeError"
    assert "Connection refused" not in result["error"]
    assert result["exception_class"] == "RuntimeError"
    assert result["exception_message"] == "Connection refused"


def test_execute_timeout():
    driver_dir = _make_driver_dir(SLOW_DRIVER)
    result = execute_driver_method(driver_dir, "status", {"HERD_device_id": "test"}, timeout=2)
    assert result["success"] is False
    assert "timed out" in result["error"]


def test_execute_context_passed():
    driver_dir = _make_driver_dir(CONTEXT_READER)
    context = {
        "HERD_device_id": "abc-123",
        "HERD_ip_address": "10.0.1.50",
        "HERD_password": "secret",
    }
    result = execute_driver_method(driver_dir, "status", context, timeout=10)
    assert result["success"] is True
    assert result["output"]["device_id"] == "abc-123"
    assert result["output"]["ip"] == "10.0.1.50"


def test_execute_nonexistent_driver():
    result = execute_driver_method("/nonexistent/path", "login", {}, timeout=5)
    assert result["success"] is False
    assert result["error"] is not None


def test_execute_default_timeout_status():
    """When timeout is None and action is 'status', uses status_check_timeout_seconds."""
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(driver_dir, "status", {"HERD_device_id": "test"})
    assert result["success"] is True
    assert result["duration_ms"] >= 0


def test_execute_default_timeout_login():
    """When timeout is None and action is not 'status', uses execution_timeout_seconds."""
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"})
    assert result["success"] is True
    assert result["duration_ms"] >= 0


def test_requirements_txt_skipped_when_pip_install_disabled(monkeypatch, caplog):
    """With allow_driver_pip_install off (the default), a requirements.txt is
    NOT installed: no _deps dir is created, the skip is logged, and the driver
    still runs."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", False)
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    reqs_path = os.path.join(driver_dir, "requirements.txt")
    with open(reqs_path, "w") as f:
        f.write("# empty requirements\n")
    with caplog.at_level("INFO"):
        result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=15)
    assert result["success"] is True
    assert not os.path.exists(os.path.join(driver_dir, "_deps"))
    assert any("allow_driver_pip_install is off" in rec.message for rec in caplog.records)


def test_requirements_txt_installed_when_pip_install_enabled(monkeypatch):
    """With allow_driver_pip_install on, the install subprocess is invoked for a
    driver that ships a requirements.txt, through `_run_child` (so nothing pip
    starts outlives it, issue #1407) with its 60 second timeout and byte output.
    `_run_child` is stubbed for the pip command so no real network install
    happens; the driver child still runs for real."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    with open(os.path.join(driver_dir, "requirements.txt"), "w") as f:
        f.write("# empty requirements\n")

    real_run = driver_sandbox._run_child
    pip_calls = []
    pip_envs = []
    pip_kwargs = []

    def fake_run(cmd, *args, **kwargs):
        if "pip" in cmd:
            pip_calls.append(cmd)
            pip_envs.append(kwargs["env"])
            pip_kwargs.append({k: v for k, v in kwargs.items() if k != "env"})

            class _Done:
                returncode = 0

            return _Done()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(driver_sandbox, "_run_child", fake_run)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=15)
    assert result["success"] is True
    assert len(pip_calls) == 1
    # pip runs in isolated mode too (issue #1180), so no `sitecustomize.py` or
    # stdlib-shadowing module in the package or the current directory can run
    # inside it, and it gets no PYTHONPATH; `-B` as for the child.
    assert pip_calls[0] == [
        "python",
        "-I",
        "-B",
        "-m",
        "pip",
        "install",
        "-r",
        os.path.join(driver_dir, "requirements.txt"),
        "--target",
        os.path.join(driver_dir, "_deps"),
        "-q",
    ]
    assert "PYTHONPATH" not in pip_envs[0]
    assert pip_kwargs[0] == {"timeout": 60, "text": False}
    # Issue #1495: pip gets PATH only, none of the device context the child gets.
    assert set(pip_envs[0]) == {"PATH"}


# --- prepare_driver_dependencies: the install, once per caller (issue #1495) ---


def _pip_fake(monkeypatch, behaviour):
    """Stub `_run_child` for the pip command only; `behaviour(cmd)` returns the
    CompletedProcess or raises. Returns the list of pip commands seen."""
    real_run = driver_sandbox._run_child
    seen = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:6] == ["python", "-I", "-B", "-m", "pip", "install"]:
            seen.append(cmd)
            return behaviour(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(driver_sandbox, "_run_child", fake_run)
    return seen


def _with_requirements(driver_code=VALID_L1_DRIVER) -> str:
    driver_dir = _make_driver_dir(driver_code)
    with open(os.path.join(driver_dir, "requirements.txt"), "w") as f:
        f.write("# empty requirements\n")
    return driver_dir


def _done(returncode):
    return subprocess.CompletedProcess(["pip"], returncode, b"", b"")


def _timed_out(cmd):
    raise subprocess.TimeoutExpired(cmd, driver_sandbox.DRIVER_PIP_INSTALL_TIMEOUT_SECONDS)


def _cannot_start(cmd):
    raise OSError("no python")


def test_the_install_timeout_is_sixty_seconds():
    """The constant the wiring claim budget counts once per action (issue #1495)."""
    assert driver_sandbox.DRIVER_PIP_INSTALL_TIMEOUT_SECONDS == 60


@pytest.mark.parametrize(
    ("flag", "has_requirements", "has_deps", "behaviour", "expected", "pip_runs"),
    [
        # Not due: no requirements.txt, or _deps already there; the flag does not matter.
        (True, False, False, None, driver_sandbox.DEPS_NOT_NEEDED, 0),
        (True, True, True, None, driver_sandbox.DEPS_NOT_NEEDED, 0),
        (False, False, False, None, driver_sandbox.DEPS_NOT_NEEDED, 0),
        (False, True, True, None, driver_sandbox.DEPS_NOT_NEEDED, 0),
        # Due but off: nothing runs.
        (False, True, False, None, driver_sandbox.DEPS_INSTALL_DISABLED, 0),
        # Due and on: one pip run, whatever it ends in.
        (True, True, False, lambda cmd: _done(0), driver_sandbox.DEPS_INSTALLED, 1),
        (True, True, False, lambda cmd: _done(1), driver_sandbox.DEPS_INSTALL_FAILED, 1),
        (True, True, False, _timed_out, driver_sandbox.DEPS_INSTALL_FAILED, 1),
        (True, True, False, _cannot_start, driver_sandbox.DEPS_INSTALL_FAILED, 1),
    ],
)
def test_prepare_driver_dependencies_answers_every_case(
    monkeypatch, flag, has_requirements, has_deps, behaviour, expected, pip_runs
):
    """The whole space of (flag, requirements.txt, _deps, pip's ending): pip runs
    exactly when the install is due and enabled, and an install that exits non-zero,
    times out, or cannot start answers `failed` and never raises."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", flag)
    driver_dir = _with_requirements() if has_requirements else _make_driver_dir(VALID_L1_DRIVER)
    if has_deps:
        os.makedirs(os.path.join(driver_dir, "_deps"))
    seen = _pip_fake(monkeypatch, behaviour or (lambda cmd: pytest.fail("pip must not run")))
    assert driver_sandbox.prepare_driver_dependencies(driver_dir) == expected
    assert len(seen) == pip_runs


@pytest.mark.parametrize("behaviour", [lambda cmd: _done(0), lambda cmd: _done(2), _timed_out])
def test_install_dependencies_false_never_runs_pip(monkeypatch, behaviour):
    """`install_dependencies=False`, what the wiring path passes on every attempt of an
    action (issue #1495): no pip run even while the install is due and enabled, and
    the driver child still runs."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    driver_dir = _with_requirements()
    seen = _pip_fake(monkeypatch, behaviour)
    result = execute_driver_method(
        driver_dir, "login", {"HERD_device_id": "test"}, timeout=15, install_dependencies=False
    )
    assert result["success"] is True
    assert seen == []


def test_the_default_still_installs_before_each_call(monkeypatch):
    """The callers that make one sandbox call per action (manual runs, health polls,
    the schema extraction, recipes) keep the default: an install that failed leaves
    no _deps, so each such call tries again."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    driver_dir = _with_requirements()
    seen = _pip_fake(monkeypatch, _timed_out)
    for _ in range(2):
        result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "t"}, timeout=15)
        assert result["success"] is True
    assert len(seen) == 2


DEPS_IMPORT_DRIVER = """
import herd_vendored_dep

class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"dep": herd_vendored_dep.VALUE, "dep_file": herd_vendored_dep.__file__}
"""


def test_execute_with_existing_deps_dir():
    """A module vendored under _deps/ is importable by the driver: the runner
    puts _deps on the child's sys.path itself (the child runs with `-I`, so no
    PYTHONPATH reaches it, issue #1180)."""
    driver_dir = _make_driver_dir(DEPS_IMPORT_DRIVER)
    deps_dir = os.path.join(driver_dir, "_deps")
    os.makedirs(deps_dir)
    with open(os.path.join(deps_dir, "herd_vendored_dep.py"), "w") as f:
        f.write('VALUE = "from _deps"\n')
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=10)
    assert result["success"] is True, result
    assert result["output"] == {
        "dep": "from _deps",
        "dep_file": os.path.join(deps_dir, "herd_vendored_dep.py"),
    }


def test_execute_connect_ports_via_method_kwargs():
    """method_kwargs passes port_a/port_b to connect_ports."""
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(
        driver_dir,
        "connect_ports",
        {"HERD_device_id": "test"},
        method_kwargs={"port_a": "0/0/1", "port_b": "0/0/2"},
        timeout=10,
    )
    assert result["success"] is True
    assert result["output"]["port_a"] == "0/0/1"
    assert result["output"]["port_b"] == "0/0/2"


def test_execute_l2_add_to_vlan():
    """L2 method_kwargs: port, vlan_id, tag passed to add_to_vlan."""
    l2_driver = """
class Driver:
    def __init__(self, context):
        self.context = context
    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        return {"port": port, "vlan_id": vlan_id, "tag": tag}
"""
    driver_dir = _make_driver_dir(l2_driver)
    result = execute_driver_method(
        driver_dir,
        "add_to_vlan",
        {"HERD_device_id": "test"},
        method_kwargs={"port": "eth1", "vlan_id": 100, "tag": "tagged"},
        timeout=10,
    )
    assert result["success"] is True
    assert result["output"]["port"] == "eth1"
    assert result["output"]["vlan_id"] == 100
    assert result["output"]["tag"] == "tagged"


def test_execute_l2_create_vlan():
    """L2 create_vlan with only vlan_id kwarg."""
    l2_driver = """
class Driver:
    def __init__(self, context):
        self.context = context
    def create_vlan(self, vlan_id):
        return {"created": True, "vlan_id": vlan_id}
"""
    driver_dir = _make_driver_dir(l2_driver)
    result = execute_driver_method(
        driver_dir,
        "create_vlan",
        {"HERD_device_id": "test"},
        method_kwargs={"vlan_id": 500},
        timeout=10,
    )
    assert result["success"] is True
    assert result["output"]["vlan_id"] == 500


def test_execute_non_json_stdout():
    """Driver that prints non-JSON stdout returns raw_output wrapper."""
    non_json_driver = """
class Driver:
    def __init__(self, context):
        pass
    def login(self):
        import sys
        # Print non-JSON to stdout, then return (but _runner prints json.dumps of the return)
        return "not-a-dict"
    def logout(self):
        return {}
    def connect_ports(self, a, b):
        return {}
    def disconnect_ports(self, a, b):
        return {}
    def status(self):
        return {}
"""
    driver_dir = _make_driver_dir(non_json_driver)
    result = execute_driver_method(driver_dir, "login", {}, timeout=10)
    # The return value is json.dumps("not-a-dict") which is valid JSON string
    assert result["success"] is True


# -- context_to_env_vars unit tests --


def test_context_to_env_vars_string():
    result = context_to_env_vars({"HERD_ip": "10.0.0.1"})
    assert result == {"HERD_IP": "10.0.0.1"}


def test_context_to_env_vars_int_and_float():
    result = context_to_env_vars({"HERD_port_count": 48, "HERD_weight": 3.5})
    assert result == {"HERD_PORT_COUNT": "48", "HERD_WEIGHT": "3.5"}


def test_context_to_env_vars_bool():
    result = context_to_env_vars({"HERD_managed": True, "HERD_disabled": False})
    assert result == {"HERD_MANAGED": "true", "HERD_DISABLED": "false"}


def test_context_to_env_vars_none():
    result = context_to_env_vars({"HERD_notes": None})
    assert result == {"HERD_NOTES": ""}


def test_context_to_env_vars_list():
    result = context_to_env_vars({"HERD_tags": ["a", "b", "c"]})
    assert result == {"HERD_TAGS": '["a", "b", "c"]'}


def test_context_to_env_vars_dict():
    result = context_to_env_vars({"HERD_meta": {"foo": 1}})
    assert result == {"HERD_META": '{"foo": 1}'}


def test_context_to_env_vars_empty():
    assert context_to_env_vars({}) == {}


def test_context_to_env_vars_exclude_strips_secrets():
    """Keys in `exclude` are dropped (matched case-insensitively against the
    uppercased env key), so device secrets are not copied into the env."""
    context = {
        "HERD_ip": "10.0.0.1",
        "HERD_login": "admin",
        "HERD_password": "secret",
        "HERD_enable_secret": "topsecret",
    }
    result = context_to_env_vars(context, exclude={"HERD_password", "HERD_enable_secret"})
    assert result == {"HERD_IP": "10.0.0.1", "HERD_LOGIN": "admin"}
    assert "HERD_PASSWORD" not in result
    assert "HERD_ENABLE_SECRET" not in result


def test_context_to_env_vars_exclude_none_keeps_everything():
    """exclude=None (the default) preserves the prior behavior: no stripping."""
    context = {"HERD_ip": "10.0.0.1", "HERD_password": "secret"}
    result = context_to_env_vars(context)
    assert result["HERD_PASSWORD"] == "secret"


def test_context_to_env_vars_keys_uppercased():
    result = context_to_env_vars(
        {
            "HERD_ip": "10.0.0.1",
            "HERD_device_id": "abc",
            "HERD_serial_number": "SN123",
        }
    )
    assert set(result.keys()) == {"HERD_IP", "HERD_DEVICE_ID", "HERD_SERIAL_NUMBER"}


def test_context_to_env_vars_mixed():
    """Full context with all supported types."""
    context = {
        "HERD_ip": "10.0.0.1",
        "HERD_login": "admin",
        "HERD_password": "secret",
        "HERD_port_count": 48,
        "HERD_managed": True,
        "HERD_location": "Santa Clara",
        "HERD_notes": None,
        "HERD_device_id": "abc-123",
        "HERD_device_name": "L1-Edge-01",
        "HERD_connection_type": "Layer 1 Switch",
        "HERD_reservation_id": None,
        "HERD_user_id": "user-456",
    }
    result = context_to_env_vars(context)
    assert result["HERD_IP"] == "10.0.0.1"
    assert result["HERD_LOGIN"] == "admin"
    assert result["HERD_PASSWORD"] == "secret"
    assert result["HERD_PORT_COUNT"] == "48"
    assert result["HERD_MANAGED"] == "true"
    assert result["HERD_LOCATION"] == "Santa Clara"
    assert result["HERD_NOTES"] == ""
    assert result["HERD_DEVICE_ID"] == "abc-123"
    assert result["HERD_DEVICE_NAME"] == "L1-Edge-01"
    assert result["HERD_CONNECTION_TYPE"] == "Layer 1 Switch"
    assert result["HERD_RESERVATION_ID"] == ""
    assert result["HERD_USER_ID"] == "user-456"


# -- Integration test: env vars visible inside subprocess --

ENV_READER = """
import os

class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {
            "env_ip": os.environ.get("HERD_IP", ""),
            "env_device_id": os.environ.get("HERD_DEVICE_ID", ""),
            "env_password": os.environ.get("HERD_PASSWORD", "MISSING"),
            "env_managed": os.environ.get("HERD_MANAGED", ""),
            "env_port_count": os.environ.get("HERD_PORT_COUNT", ""),
            "env_notes": os.environ.get("HERD_NOTES", "MISSING"),
            "context_ip": self.context.get("HERD_ip", ""),
            "context_device_id": self.context.get("HERD_device_id", ""),
            "context_password": self.context.get("HERD_password", "MISSING"),
        }

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        return {"reachable": True}
"""


def test_env_vars_visible_in_subprocess():
    """Non-secret HERD_ context values are available as uppercase env vars in
    the driver subprocess (the observability convenience)."""
    driver_dir = _make_driver_dir(ENV_READER)
    context = {
        "HERD_ip": "10.0.0.1",
        "HERD_device_id": "abc-123",
        "HERD_password": "secret",
        "HERD_managed": True,
        "HERD_port_count": 48,
        "HERD_notes": None,
    }
    result = execute_driver_method(driver_dir, "login", context, timeout=10)
    assert result["success"] is True
    output = result["output"]
    # Env vars (uppercased keys, string values)
    assert output["env_ip"] == "10.0.0.1"
    assert output["env_device_id"] == "abc-123"
    assert output["env_managed"] == "true"
    assert output["env_port_count"] == "48"
    assert output["env_notes"] == ""
    # Context dict still works (original mixed-case keys via JSON file)
    assert output["context_ip"] == "10.0.0.1"
    assert output["context_device_id"] == "abc-123"


def test_password_keys_stripped_from_env_but_kept_in_context():
    """When password_keys is provided, secret values are NOT copied into the
    child env (no /proc/<pid>/environ exposure), but the driver still receives
    them via the context temp file so it can authenticate."""
    driver_dir = _make_driver_dir(ENV_READER)
    context = {
        "HERD_ip": "10.0.0.1",
        "HERD_device_id": "abc-123",
        "HERD_password": "secret",
    }
    result = execute_driver_method(
        driver_dir,
        "login",
        context,
        timeout=10,
        password_keys={"HERD_password"},
    )
    assert result["success"] is True
    output = result["output"]
    # Secret is absent from the env (default "MISSING" sentinel comes back).
    assert output["env_password"] == "MISSING"
    # Non-secret env vars are unaffected.
    assert output["env_ip"] == "10.0.0.1"
    # The driver still gets the password from the context dict.
    assert output["context_password"] == "secret"


# -- Every process the run started ends with it (issue #1407) --
#
# These run real children. GROUP_DRIVER starts one grandchild (a Python process
# that ignores SIGTERM and sleeps, its command line marked `herd1407-grandchild`)
# and records its own pid, group, and session, and the grandchild's pid and start
# time, to a file, then ends the way `HERD_mode` says. The start time lets a test
# tell the grandchild from a later process that reuses its pid.

GROUP_DRIVER = """
import json
import os
import signal
import subprocess
import sys
import time

_GRANDCHILD = [
    sys.executable,
    "-I",
    "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
    "herd1407-grandchild",
]


def _start_time(pid):
    with open(f"/proc/{pid}/stat") as f:
        return int(f.read().rsplit(")", 1)[1].split()[19])


class Driver:
    def __init__(self, context):
        self.context = context

    def run(self):
        mode = self.context["HERD_mode"]
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        streams = {"stdin": subprocess.DEVNULL}
        if mode != "hold_pipes":
            streams.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        grandchild = subprocess.Popen(
            _GRANDCHILD, start_new_session=(mode == "escape"), **streams
        )
        record = {
            "child": os.getpid(),
            "pgid": os.getpgrp(),
            "sid": os.getsid(0),
            "grandchild": grandchild.pid,
            "grandchild_start": _start_time(grandchild.pid),
        }
        path = self.context["HERD_record"]
        with open(path + ".tmp", "w") as f:
            json.dump(record, f)
        os.replace(path + ".tmp", path)
        if mode == "hang":
            time.sleep(120)
        if mode == "raise":
            raise RuntimeError("helper left behind at 10.9.9.9")
        if mode == "die":
            os.kill(os.getpid(), signal.SIGKILL)
        return {"ok": True}
"""

# Generous: a passing run sees every condition within milliseconds; the bound
# only ends a failing run.
_WITHIN_SECONDS = 30.0


def _proc_state(pid: int, start_time: int) -> str:
    """'gone', 'zombie', or 'alive' for the process `pid` that started at
    `start_time` (clock ticks after boot, /proc/<pid>/stat field 22)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return "gone"
    if int(fields[19]) != start_time:
        return "gone"  # the pid now names another process
    return "zombie" if fields[0] == "Z" else "alive"


def _wait_until_gone(pid: int, start_time: int) -> str:
    """Poll until the process is gone (killed AND reaped by whoever adopted it),
    or the bound passes; return the last state seen."""
    deadline = time.monotonic() + _WITHIN_SECONDS
    while (state := _proc_state(pid, start_time)) != "gone" and time.monotonic() < deadline:
        time.sleep(0.01)
    return state


def _wait_for_record(path: str) -> dict:
    deadline = time.monotonic() + _WITHIN_SECONDS
    while not os.path.exists(path):
        assert time.monotonic() < deadline, "the driver never wrote its record"
        time.sleep(0.01)
    with open(path) as f:
        return json.load(f)


def _assert_child_reaped(child_pid: int) -> None:
    """The sandbox child is no longer a child of this process: it was reaped,
    so it is neither running nor a zombie."""
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pid, os.WNOHANG)


def _cleanup(record_path: str) -> None:
    """Kill whatever a test's run left alive, even when the test failed."""
    if not os.path.exists(record_path):
        return
    with open(record_path) as f:
        record = json.load(f)
    with contextlib.suppress(ChildProcessError):
        if os.waitpid(record["child"], os.WNOHANG) == (0, 0):
            os.kill(record["child"], signal.SIGKILL)
            os.waitpid(record["child"], 0)
    if _proc_state(record["grandchild"], record["grandchild_start"]) == "alive":
        with contextlib.suppress(ProcessLookupError):
            os.kill(record["grandchild"], signal.SIGKILL)
    _wait_until_gone(record["grandchild"], record["grandchild_start"])


@pytest.fixture
def group_run(monkeypatch, tmp_path):
    """(driver dir, context for a mode, record path); cleans up after the test."""
    # The drivers fork; RLIMIT_NPROC counts every process and thread of this
    # host's user, which is not what these tests are about.
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nproc", 0)
    driver_dir = _make_driver_dir(GROUP_DRIVER)
    record_path = str(tmp_path / "record.json")

    def context(mode: str) -> dict:
        return {"HERD_mode": mode, "HERD_record": record_path}

    yield driver_dir, context, record_path
    _cleanup(record_path)


def _spy_on_group_kills(monkeypatch) -> list[tuple[int, int, bool]]:
    """Record every os.killpg call as (group id, signal, child reaped yet), then
    pass it on. 'Reaped' is read for the process whose pid is the group id."""
    real_killpg = os.killpg
    calls: list[tuple[int, int, bool]] = []

    def spy(pgid, sig):
        try:
            os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            reaped = False
        except ChildProcessError:
            reaped = True
        calls.append((pgid, sig, reaped))
        real_killpg(pgid, sig)

    monkeypatch.setattr(driver_sandbox.os, "killpg", spy)
    return calls


_ENDINGS = {
    # mode: (timeout, the result's expected (success, error, output))
    "return": (20, (True, None, {"ok": True})),
    "hold_pipes": (20, (True, None, {"ok": True})),
    "raise": (20, (False, "driver raised RuntimeError", None)),
    "die": (20, (False, "driver killed by signal 9 (likely a resource limit)", None)),
    "hang": (5, (False, "Execution timed out after 5s", None)),
}


@pytest.mark.parametrize(
    "mode",
    list(_ENDINGS),
    ids=[
        "exit-0",
        "exit-0-helper-holds-the-pipes",
        "exit-1-driver-raised",
        "killed-by-a-signal",
        "timeout-driver-ignores-sigterm",
    ],
)
def test_no_process_the_run_started_outlives_it(group_run, monkeypatch, mode):
    """For each way the child can end, the grandchild it started is gone (killed
    and reaped) once the call returns, the child is reaped, and every group kill
    went to the child's own group, as SIGKILL, before the child was reaped (the
    order that rules out a reused id). The result is the one an honest run of the
    same ending gets: a helper holding the pipes no longer turns a successful
    call into a timeout, and the driver's text never reaches `error`."""
    driver_dir, context, record_path = group_run
    timeout, (success, error, output) = _ENDINGS[mode]
    kills = _spy_on_group_kills(monkeypatch)

    result = execute_driver_method(driver_dir, "run", context(mode), timeout=timeout)

    record = _wait_for_record(record_path)
    assert (result["success"], result["error"], result["output"]) == (success, error, output)
    assert _wait_until_gone(record["grandchild"], record["grandchild_start"]) == "gone"
    _assert_child_reaped(record["child"])
    assert kills, "no group kill was sent"
    assert all(call == (record["child"], signal.SIGKILL, False) for call in kills), kills
    # The exit watcher was joined before the call returned.
    watchers = [t for t in threading.enumerate() if t.name == driver_sandbox._WATCHER_THREAD_NAME]
    assert watchers == []


@pytest.mark.timeout(30)
def test_the_child_is_killed_even_if_the_group_kill_does_nothing(group_run, monkeypatch):
    """Belt and braces: at the timeout the child itself is sent SIGKILL besides
    its group, so a group kill that did nothing (here stubbed out) cannot leave
    the call waiting on a child that ignores SIGTERM. Without that kill the call
    would end only when the driver's 120 second sleep does; the duration bound
    (far below the sleep, far above the 3 second timeout) and the 30 second
    marker make that fail fast. The grandchild survives the stub; the fixture
    kills it."""
    driver_dir, context, record_path = group_run
    monkeypatch.setattr(driver_sandbox, "_kill_group", lambda pgid: None)
    result = execute_driver_method(driver_dir, "run", context("hang"), timeout=3)
    assert result["error"] == "Execution timed out after 3s"
    assert result["duration_ms"] < 30_000
    record = _wait_for_record(record_path)
    _assert_child_reaped(record["child"])


def test_child_leads_its_own_session_and_group(group_run):
    """The child is a session leader: its group and session ids are its pid, not
    this process's group, so a kill of its group can never reach the service."""
    driver_dir, context, record_path = group_run
    result = execute_driver_method(driver_dir, "run", context("return"), timeout=20)
    assert result["success"] is True, result
    record = _wait_for_record(record_path)
    assert record["pgid"] == record["sid"] == record["child"]
    assert record["pgid"] != os.getpgrp()


class _Interrupted(BaseException):
    """Raised in the parent between the spawn and the reap, standing in for a
    KeyboardInterrupt or any error the parent hits while it waits."""


@pytest.mark.parametrize("mode", ["return", "hang"], ids=["child-exited", "child-running"])
def test_an_exception_in_the_parent_after_the_spawn_still_kills_the_group(
    group_run, monkeypatch, mode
):
    """An exception raised in the parent while it waits on the child (here from
    `_collect`, once the grandchild exists) propagates, and on its way out the
    group is killed and the child reaped, whether the child had exited already
    or was still running."""
    driver_dir, context, record_path = group_run

    def interrupted_collect(proc, exit_r, pgid, deadline):
        _wait_for_record(record_path)
        raise _Interrupted

    monkeypatch.setattr(driver_sandbox, "_collect", interrupted_collect)
    with pytest.raises(_Interrupted):
        execute_driver_method(driver_dir, "run", context(mode), timeout=60)

    record = _wait_for_record(record_path)
    assert _wait_until_gone(record["grandchild"], record["grandchild_start"]) == "gone"
    _assert_child_reaped(record["child"])


def test_the_exit_watcher_is_joined_before_the_call_returns(monkeypatch):
    """The exit watcher is joined before the child is reaped, so it can never
    wait on a pid that a later process reuses. The watcher is held back for half
    a second (the injected fault that opens the window) and the parent raises
    before it sees the exit; the call must still not return while it runs.

    The hold is a fixed sleep, not a gate, by necessity: with the join, the call
    returns only after the watcher ends, so whatever releases the watcher must
    fire inside the call, before the join; without the join, that same release
    would let the watcher end before the check below, and the test could not
    tell the two apart. A gate released by the test after the call returns would
    deadlock the correct code. The sleep only widens the window; no assertion
    reads a clock."""
    real_watch = driver_sandbox._watch_exit

    def late_watch(pid, exit_w):
        time.sleep(0.5)
        real_watch(pid, exit_w)

    def interrupted_collect(proc, exit_r, pgid, deadline):
        raise _Interrupted

    monkeypatch.setattr(driver_sandbox, "_watch_exit", late_watch)
    monkeypatch.setattr(driver_sandbox, "_collect", interrupted_collect)
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    with pytest.raises(_Interrupted):
        execute_driver_method(driver_dir, "login", {}, timeout=20)
    watchers = [t for t in threading.enumerate() if t.name == driver_sandbox._WATCHER_THREAD_NAME]
    assert watchers == []


async def test_a_cancelled_caller_does_not_leave_the_group_running(group_run):
    """Callers run the sandbox on a worker thread. Cancelling the awaiting task
    does not stop that thread: the call runs on to its timeout, which kills the
    group and reaps the child, as without the cancellation."""
    driver_dir, context, record_path = group_run
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = loop.run_in_executor(
            pool,
            functools.partial(execute_driver_method, driver_dir, "run", context("hang"), timeout=3),
        )
        await asyncio.to_thread(_wait_for_record, record_path)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        # Leaving the block waits for the worker thread, so the call has ended.
    record = _wait_for_record(record_path)
    assert _wait_until_gone(record["grandchild"], record["grandchild_start"]) == "gone"
    _assert_child_reaped(record["child"])


def test_limit_a_grandchild_in_a_session_of_its_own_survives(group_run):
    """The documented limit, recorded as a fact: a process that leaves the
    child's group (here a new session, as a self-daemonizing helper makes) is
    not killed by the group kill. It holds neither pipe, so the call itself ends
    normally. The fixture kills it."""
    driver_dir, context, record_path = group_run
    result = execute_driver_method(driver_dir, "run", context("escape"), timeout=20)
    assert result["success"] is True, result
    record = _wait_for_record(record_path)
    _assert_child_reaped(record["child"])
    assert _proc_state(record["grandchild"], record["grandchild_start"]) == "alive"


HONEST_DRIVER = """
import sys

class Driver:
    def __init__(self, context):
        pass

    def login(self):
        sys.stderr.buffer.write(_STDERR_BYTES)
        sys.stderr.buffer.flush()
        return {"success": True, "text": "x\\r\\ny"}
"""

# What the honest driver writes to stderr: both newline forms `text=True`
# translates, and a non-ASCII letter in UTF-8 (u with diaeresis).
_HONEST_STDERR = b"a\r\nb\rc Z\xc3\xbcrich\n"
HONEST_DRIVER = HONEST_DRIVER.replace("_STDERR_BYTES", repr(_HONEST_STDERR))


def _reference_run(stream: str, data: bytes) -> subprocess.CompletedProcess:
    """What `subprocess.run(text=True)`, the sandbox's old spawn, makes of a child
    that writes `data` to `stream`, decoded in this process's locale."""
    script = f"import sys; sys.{stream}.buffer.write({data!r})"
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)


def test_an_honest_driver_gets_the_same_result_as_before():
    """An honest driver's result has the shape subprocess.run(text=True) gave it:
    stdout parsed as JSON, stderr decoded in the locale's encoding with universal
    newlines (CRLF and CR become LF), non-ASCII text intact, no error, an empty
    transcript. The expected stderr is also what subprocess.run(text=True) makes
    of the same bytes here, so the comparison is with the old spawn, not with a
    hand-written decoding (a UTF-8 locale is assumed, as on this host and CI)."""
    expected_stderr = "a\nb\nc Z" + chr(0xFC) + "rich\n"
    assert _reference_run("stderr", _HONEST_STDERR).stderr == expected_stderr
    driver_dir = _make_driver_dir(HONEST_DRIVER)
    result = execute_driver_method(driver_dir, "login", {}, timeout=20)
    duration = result.pop("duration_ms")
    assert isinstance(duration, int) and duration >= 0
    assert result == {
        "success": True,
        "output": {"success": True, "text": "x\r\ny"},
        "error": None,
        "stderr": expected_stderr,
        "transcript": [],
    }


INVALID_TEXT_DRIVER = """
import sys

class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        sys.stdout.buffer.write(b"\\xff\\n")
        sys.stdout.buffer.flush()
        return {"success": True}
"""


def test_output_that_is_not_valid_text_raises_as_subprocess_run_did():
    """A driver whose stdout is not valid in the locale's encoding makes
    `execute_driver_method` raise UnicodeDecodeError, the error
    subprocess.run(text=True) raised for the same byte. The sandbox maps nothing
    here, as before; on the run path `run_driver_action` records it as
    `execution failed: UnicodeDecodeError` (`_fail_run_unexpected`)."""
    with pytest.raises(UnicodeDecodeError) as reference:
        _reference_run("stdout", b"\xff\n")
    driver_dir = _make_driver_dir(INVALID_TEXT_DRIVER)
    with pytest.raises(UnicodeDecodeError) as raised:
        execute_driver_method(driver_dir, "login", {}, timeout=20)
    ours, theirs = raised.value, reference.value
    assert (ours.encoding, ours.reason) == (theirs.encoding, theirs.reason)
    assert ours.object[ours.start : ours.end] == theirs.object[theirs.start : theirs.end]
    assert ours.object[ours.start : ours.end] == b"\xff"


def test_child_spawn_starts_a_new_session_without_preexec_fn(monkeypatch):
    """The child is started by Popen with start_new_session (the session is made
    in the child before exec, so before any kill) and no preexec_fn, which is not
    safe in the threads the sandbox runs on (#307). What Popen itself receives is
    also the command of CFG-SBX-1 and the environment of CFG-SBX-3, exactly: the
    tests that stub `_run_child` see only what reaches that function, so a spawn
    that widened the environment there (`{**os.environ, **env}`) passed them."""
    real_popen = driver_sandbox.subprocess.Popen
    seen = []

    def spy(cmd, *args, **kwargs):
        seen.append((list(cmd), kwargs))
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(driver_sandbox.subprocess, "Popen", spy)
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(driver_dir, "login", {}, timeout=20)
    assert result["success"] is True, result
    assert len(seen) == 1
    cmd, kwargs = seen[0]
    assert cmd[:6] == ["python", "-I", "-B", driver_sandbox._RUNNER_PATH, driver_dir, "login"]
    assert len(cmd) == 7  # plus the context file; no kwargs file
    assert os.path.basename(cmd[6]).startswith("herd_ctx_")
    assert set(kwargs["env"]) == {"PATH", "HERD_TRANSCRIPT_PATH", "HERD_SANDBOX_RLIMITS"}
    assert kwargs["start_new_session"] is True
    assert kwargs.get("preexec_fn") is None


def test_the_deadline_is_the_timeout_from_the_spawn(monkeypatch):
    """The read gives up `timeout` seconds after the spawn: the deadline handed
    to `_collect` is within a second of now plus the timeout, and never past it.
    No duration is measured, so the bound holds on any host speed."""
    real_collect = driver_sandbox._collect
    seen = []

    def spy(proc, exit_r, pgid, deadline):
        seen.append(deadline - time.monotonic())
        return real_collect(proc, exit_r, pgid, deadline)

    monkeypatch.setattr(driver_sandbox, "_collect", spy)
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    result = execute_driver_method(driver_dir, "login", {}, timeout=20)
    assert result["success"] is True, result
    assert len(seen) == 1
    assert 19 < seen[0] <= 20


def _open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_no_descriptor_or_thread_is_left_after_any_ending(monkeypatch, tmp_path):
    """Every ending (exit 0, exit 0 with a helper holding the pipes, a raised
    driver exception, death by a signal, the timeout, and an exception in the
    parent) closes every descriptor the call opened (both output pipes and both
    ends of the exit pipe) and leaves no thread behind. One round of every ending
    warms up whatever opens a descriptor once; two more rounds must leave the
    counts where they were."""
    monkeypatch.setattr(driver_sandbox.settings, "driver_rlimit_nproc", 0)
    driver_dir = _make_driver_dir(GROUP_DRIVER)
    records: list[str] = []
    endings = [("return", 20), ("hold_pipes", 20), ("raise", 20), ("die", 20), ("hang", 1)]

    def interrupted_collect(proc, exit_r, pgid, deadline):
        raise _Interrupted

    def one_round(tag: str) -> None:
        for mode, timeout in endings:
            record = str(tmp_path / f"{tag}-{mode}.json")
            records.append(record)
            context = {"HERD_mode": mode, "HERD_record": record}
            execute_driver_method(driver_dir, "run", context, timeout=timeout)
        record = str(tmp_path / f"{tag}-interrupted.json")
        records.append(record)
        with monkeypatch.context() as patch:
            patch.setattr(driver_sandbox, "_collect", interrupted_collect)
            with pytest.raises(_Interrupted):
                execute_driver_method(
                    driver_dir, "run", {"HERD_mode": "return", "HERD_record": record}, timeout=20
                )

    try:
        one_round("warm-up")
        descriptors, threads = _open_descriptors(), threading.active_count()
        one_round("first")
        one_round("second")
        assert (_open_descriptors(), threading.active_count()) == (descriptors, threads)
    finally:
        for record in records:
            _cleanup(record)


def test_kill_group_refuses_this_process_group_and_ids_below_two(monkeypatch):
    """`_kill_group` never signals the service's own group, 0 (which means the
    caller's group), 1, or a negative id; any other id gets SIGKILL."""
    calls = []
    monkeypatch.setattr(driver_sandbox.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    for refused in (os.getpgrp(), 0, 1, -1):
        driver_sandbox._kill_group(refused)
    assert calls == []
    other = os.getpgrp() + 1
    driver_sandbox._kill_group(other)
    assert calls == [(other, signal.SIGKILL)]


def _sh_child(script: str, *, timeout: float, text: bool = False):
    return driver_sandbox._run_child(
        ["sh", "-c", script], env={"PATH": os.environ["PATH"]}, timeout=timeout, text=text
    )


def _sh_with_helper(out, then: str) -> str:
    """An sh script that starts a background `sleep` (in sh's process group, its
    stdout the child's), first records sh's and the sleep's pid and start time
    to `out`, so a test that fails at any point can still kill both, then runs
    `then`."""
    record = (
        'echo $$ $(cut -d" " -f22 /proc/$$/stat) $! $(cut -d" " -f22 /proc/$!/stat)'
        f" > {out}.tmp && mv {out}.tmp {out}"
    )
    return f"sleep 120 & {record}; {then}"


def _sh_record(out) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """((sh pid, start), (helper pid, start)) from the record, or None."""
    if not out.exists():
        return None
    sh_pid, sh_start, helper_pid, helper_start = (int(v) for v in out.read_text().split())
    return (sh_pid, sh_start), (helper_pid, helper_start)


def _kill_sh_leftovers(out) -> None:
    """Kill the recorded sh and its helper if either is still alive, and reap
    the sh if it is still this process's child."""
    record = _sh_record(out)
    if record is None:
        return
    for pid, start in record:
        if _proc_state(pid, start) == "alive":
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(record[0][0], 0)
    _wait_until_gone(*record[1])


def test_run_child_bytes_mode_returns_when_a_helper_holds_the_pipes(tmp_path):
    """`_run_child` as the pip install uses it (bytes): a child that exits 0 with
    a background helper still holding stdout returns at once with its output,
    and the helper is killed with the group."""
    out = tmp_path / "record"
    try:
        result = _sh_child(_sh_with_helper(out, "echo done"), timeout=20)
        assert result.returncode == 0
        assert (result.stdout, result.stderr) == (b"done\n", b"")
        _sh, helper = _sh_record(out)
        assert _wait_until_gone(*helper) == "gone"
    finally:
        _kill_sh_leftovers(out)


def test_run_child_timeout_kills_the_group_and_raises(tmp_path):
    """At the timeout `_run_child` kills the group, reaps the child, and raises
    subprocess.TimeoutExpired, as subprocess.run did, for the pip install too."""
    out = tmp_path / "record"
    try:
        with pytest.raises(driver_sandbox.subprocess.TimeoutExpired) as raised:
            _sh_child(_sh_with_helper(out, "wait"), timeout=2)
        assert raised.value.timeout == 2
        (sh_pid, _sh_start), helper = _sh_record(out)
        assert _wait_until_gone(*helper) == "gone"
        _assert_child_reaped(sh_pid)
    finally:
        _kill_sh_leftovers(out)
