"""Unit tests for the checked-in mock Layer 1 switch driver.

Pure, stack-free: load drivers/mock_l1/driver.py by path and exercise the Driver
class directly. Guards the contract the execution sandbox depends on (the L1
method set, return shapes, dry-run, and the failure-injection knobs) without
needing Docker.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "drivers" / "mock_l1"
_DRIVER_PATH = _DRIVER_DIR / "driver.py"

# The execution loader (driver_loader.validate_driver) requires exactly these
# methods for a "Layer 1 Switch" driver; pin them here so a rename breaks in this
# fast unit test rather than only in a stack-only integration run.
L1_REQUIRED_METHODS = (
    "login",
    "logout",
    "connect_ports",
    "disconnect_ports",
    "status",
)


@pytest.fixture(scope="module")
def driver_cls():
    spec = importlib.util.spec_from_file_location("mock_l1_driver", _DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver


def test_exposes_the_l1_method_set(driver_cls):
    d = driver_cls({})
    for name in L1_REQUIRED_METHODS:
        assert callable(getattr(d, name)), f"missing required L1 method {name}"


def test_metadata_declares_l1_and_dry_run():
    meta = json.loads((_DRIVER_DIR / "driver_metadata.json").read_text())
    assert meta["connection_type"] == "Layer 1 Switch"
    assert meta["supports_dry_run"] is True


def test_port_ops_return_success_and_echo_kwargs(driver_cls):
    d = driver_cls({})
    assert d.login() == {"success": True, "output": {}}
    assert d.connect_ports(port_a="1", port_b="2") == {
        "success": True,
        "output": {"port_a": "1", "port_b": "2"},
    }
    assert d.disconnect_ports(port_a="1", port_b="2") == {
        "success": True,
        "output": {"port_a": "1", "port_b": "2"},
    }
    assert d.logout() == {"success": True, "output": {}}


def test_dry_run_flags_simulated(driver_cls):
    res = driver_cls({"dry_run": True}).connect_ports(port_a="1", port_b="2")
    assert res["success"] is True
    assert res["simulated"] is True
    assert res["output"] == {"port_a": "1", "port_b": "2"}


def test_status_reports_reachable_and_never_raises(driver_cls):
    assert driver_cls({}).status() == {"reachable": True, "simulated": False}
    assert driver_cls({"HERD_mock_fail_actions": "status"}).status()["reachable"] is False


def test_fail_injection_returns_unsuccessful_result(driver_cls):
    d = driver_cls({"HERD_mock_fail_actions": "connect_ports"})
    fail = d.connect_ports(port_a="1", port_b="2")
    assert fail["success"] is False
    assert "injected" in fail["error"]
    # An action not in the injected set still succeeds.
    assert d.disconnect_ports(port_a="1", port_b="2")["success"] is True


def test_raise_injection_raises(driver_cls):
    d = driver_cls({"HERD_mock_raise_actions": "connect_ports"})
    with pytest.raises(RuntimeError, match="injected raise on connect_ports"):
        d.connect_ports(port_a="1", port_b="2")
