import os
import tempfile

from app.services import driver_sandbox
from app.services.driver_sandbox import context_to_env_vars, execute_driver_method


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
    driver_dir = _make_driver_dir(FAILING_DRIVER)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=10)
    assert result["success"] is False
    assert "Connection refused" in result["error"]


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
    driver that ships a requirements.txt. subprocess.run is stubbed so no real
    network install happens."""
    monkeypatch.setattr(driver_sandbox.settings, "allow_driver_pip_install", True)
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    with open(os.path.join(driver_dir, "requirements.txt"), "w") as f:
        f.write("# empty requirements\n")

    real_run = driver_sandbox.subprocess.run
    pip_calls = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["pip", "install"]:
            pip_calls.append(cmd)

            class _Done:
                returncode = 0

            return _Done()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(driver_sandbox.subprocess, "run", fake_run)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=15)
    assert result["success"] is True
    assert len(pip_calls) == 1
    assert pip_calls[0][:2] == ["pip", "install"]


def test_execute_with_existing_deps_dir():
    """When _deps directory already exists, PYTHONPATH includes it."""
    driver_dir = _make_driver_dir(VALID_L1_DRIVER)
    deps_dir = os.path.join(driver_dir, "_deps")
    os.makedirs(deps_dir)
    result = execute_driver_method(driver_dir, "login", {"HERD_device_id": "test"}, timeout=10)
    assert result["success"] is True


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
