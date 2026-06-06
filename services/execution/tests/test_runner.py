"""Tests for _runner.py: standalone subprocess entry point for driver execution."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

RUNNER_PATH = str(Path(__file__).resolve().parent.parent / "app" / "services" / "_runner.py")


def _make_driver_dir(driver_code: str) -> tempfile.TemporaryDirectory:
    """Create a temp dir with a driver.py file."""
    tmpdir = tempfile.TemporaryDirectory()
    driver_py = Path(tmpdir.name) / "driver.py"
    driver_py.write_text(driver_code)
    return tmpdir


BASIC_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self):
        return {"success": True, "message": "logged in"}
    def logout(self):
        return {"success": True}
    def status(self):
        return {"reachable": True}
    def connect_ports(self, port_a, port_b):
        return {"connected": [port_a, port_b]}
    def disconnect_ports(self, port_a, port_b):
        return {"disconnected": [port_a, port_b]}
"""

FAILING_DRIVER = """
class Driver:
    def __init__(self, context):
        raise ValueError("Init failed")
"""


def _run_runner(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, RUNNER_PATH, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_runner_insufficient_args():
    result = _run_runner([])
    assert result.returncode == 1
    assert "Usage" in result.stderr


def test_runner_login_action():
    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx = {"device_id": "test-123", "ip": "10.0.0.1"}
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps(ctx))
        result = _run_runner([tmpdir.name, "login", str(ctx_file)])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["success"] is True
    assert output["message"] == "logged in"


def test_runner_status_action():
    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({"device_id": "d1"}))
        result = _run_runner([tmpdir.name, "status", str(ctx_file)])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["reachable"] is True


def test_runner_connect_ports_with_args():
    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        kwargs_json = json.dumps({"port_a": "1/0/1", "port_b": "1/0/2"})
        result = _run_runner([tmpdir.name, "connect_ports", str(ctx_file), kwargs_json])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["connected"] == ["1/0/1", "1/0/2"]


def test_runner_disconnect_ports_with_args():
    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        kwargs_json = json.dumps({"port_a": "a", "port_b": "b"})
        result = _run_runner([tmpdir.name, "disconnect_ports", str(ctx_file), kwargs_json])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["disconnected"] == ["a", "b"]


def test_runner_l2_add_to_vlan_subprocess():
    l2_driver = """
class Driver:
    def __init__(self, context):
        self.context = context
    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        return {"port": port, "vlan_id": vlan_id, "tag": tag}
"""
    tmpdir = _make_driver_dir(l2_driver)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        kwargs_json = json.dumps({"port": "eth6", "vlan_id": 200, "tag": "tagged"})
        result = _run_runner([tmpdir.name, "add_to_vlan", str(ctx_file), kwargs_json])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["port"] == "eth6"
    assert output["vlan_id"] == 200


def test_runner_invalid_driver_path():
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w") as ctx_file:
        ctx_file.write("{}")
        ctx_file.flush()
        result = _run_runner(["/nonexistent/path", "login", ctx_file.name])
    assert result.returncode == 1


def test_runner_invalid_context_file():
    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        result = _run_runner([tmpdir.name, "login", "/nonexistent/context.json"])
    assert result.returncode == 1


def test_runner_driver_init_exception():
    tmpdir = _make_driver_dir(FAILING_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        result = _run_runner([tmpdir.name, "login", str(ctx_file)])
    assert result.returncode == 1
    assert "Init failed" in result.stderr


# --- In-process tests for coverage of _runner.main() ---


def test_runner_main_insufficient_args():
    """main() exits with code 1 when fewer than 4 args are provided."""
    from app.services._runner import main

    with (
        tempfile.TemporaryDirectory() as tmpdir,
        pytest.raises(SystemExit) as exc_info,
    ):
        sys.argv = ["_runner.py", tmpdir]
        main()
    assert exc_info.value.code == 1


def test_runner_main_login():
    """main() loads the driver, calls login, and prints JSON output."""
    from app.services._runner import main

    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({"device_id": "d1"}))
        sys.argv = ["_runner.py", tmpdir.name, "login", str(ctx_file)]
        # Capture stdout
        import io

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            main()
        finally:
            sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["success"] is True
        assert output["message"] == "logged in"


def test_runner_main_connect_ports():
    """main() passes method_kwargs to connect_ports."""
    from app.services._runner import main

    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        kwargs_json = json.dumps({"port_a": "0/1/1", "port_b": "0/1/2"})
        sys.argv = [
            "_runner.py",
            tmpdir.name,
            "connect_ports",
            str(ctx_file),
            kwargs_json,
        ]
        import io

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            main()
        finally:
            sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["connected"] == ["0/1/1", "0/1/2"]


def test_runner_main_status_no_ports():
    """main() calls status without port args."""
    from app.services._runner import main

    tmpdir = _make_driver_dir(BASIC_DRIVER)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        sys.argv = ["_runner.py", tmpdir.name, "status", str(ctx_file)]
        import io

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            main()
        finally:
            sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["reachable"] is True


def test_runner_main_l2_add_to_vlan():
    """main() passes L2 method_kwargs to add_to_vlan."""
    l2_driver = """
class Driver:
    def __init__(self, context):
        self.context = context
    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        return {"port": port, "vlan_id": vlan_id, "tag": tag}
"""
    from app.services._runner import main

    tmpdir = _make_driver_dir(l2_driver)
    with tmpdir:
        ctx_file = Path(tmpdir.name) / "context.json"
        ctx_file.write_text(json.dumps({}))
        kwargs_json = json.dumps({"port": "eth1", "vlan_id": 100, "tag": "tagged"})
        sys.argv = ["_runner.py", tmpdir.name, "add_to_vlan", str(ctx_file), kwargs_json]
        import io

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            main()
        finally:
            sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["port"] == "eth1"
        assert output["vlan_id"] == 100
        assert output["tag"] == "tagged"
