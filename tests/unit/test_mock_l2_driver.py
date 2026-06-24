"""Unit tests for the checked-in mock Layer 2 switch driver.

Pure, stack-free: load drivers/mock_l2/driver.py by path and exercise the Driver
class directly. Guards the contract the execution sandbox depends on (the L2
method set, return shapes, dry-run, and the failure-injection knobs the VLAN
integration tests rely on) without needing Docker.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "drivers" / "mock_l2"
_DRIVER_PATH = _DRIVER_DIR / "driver.py"

# The execution loader (driver_loader.validate_driver) requires exactly these
# methods for a "Layer 2 Switch" driver; pin them here so a rename breaks in this
# fast unit test rather than only in a stack-only integration run.
L2_REQUIRED_METHODS = (
    "login",
    "logout",
    "create_vlan",
    "add_to_vlan",
    "remove_from_vlan",
    "delete_vlan",
    "status",
)


@pytest.fixture(scope="module")
def driver_cls():
    spec = importlib.util.spec_from_file_location("mock_l2_driver", _DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver


def test_exposes_the_l2_method_set(driver_cls):
    d = driver_cls({})
    for name in L2_REQUIRED_METHODS:
        assert callable(getattr(d, name)), f"missing required L2 method {name}"


def test_metadata_declares_l2_and_dry_run():
    meta = json.loads((_DRIVER_DIR / "driver_metadata.json").read_text())
    assert meta["connection_type"] == "Layer 2 Switch"
    assert meta["supports_dry_run"] is True


def test_vlan_ops_return_success_and_echo_kwargs(driver_cls):
    d = driver_cls({})
    assert d.login() == {"success": True, "output": {}}
    assert d.create_vlan(vlan_id=100) == {"success": True, "output": {"vlan_id": 100}}
    assert d.add_to_vlan(port="eth1", vlan_id=100, tag="tagged") == {
        "success": True,
        "output": {"port": "eth1", "vlan_id": 100, "tag": "tagged"},
    }
    assert d.remove_from_vlan(port="eth1", vlan_id=100) == {
        "success": True,
        "output": {"port": "eth1", "vlan_id": 100},
    }
    assert d.delete_vlan(vlan_id=100) == {"success": True, "output": {"vlan_id": 100}}
    assert d.logout() == {"success": True, "output": {}}


def test_dry_run_flags_simulated(driver_cls):
    res = driver_cls({"dry_run": True}).create_vlan(vlan_id=7)
    assert res["success"] is True
    assert res["simulated"] is True
    assert res["output"] == {"vlan_id": 7}


def test_status_reports_reachable_and_never_raises(driver_cls):
    assert driver_cls({}).status() == {"reachable": True, "simulated": False}
    # Even with failure injection, status answers rather than raising.
    assert driver_cls({"HERD_mock_fail_actions": "status"}).status()["reachable"] is False


def test_fail_injection_returns_unsuccessful_result(driver_cls):
    d = driver_cls({"HERD_mock_fail_actions": "create_vlan,add_to_vlan"})
    fail = d.create_vlan(vlan_id=5)
    assert fail["success"] is False
    assert "injected" in fail["error"]
    assert d.add_to_vlan(port="eth1", vlan_id=5, tag="tagged")["success"] is False
    # An action not in the injected set still succeeds.
    assert d.delete_vlan(vlan_id=5)["success"] is True


def test_raise_injection_raises(driver_cls):
    d = driver_cls({"HERD_mock_raise_actions": "add_to_vlan"})
    with pytest.raises(RuntimeError, match="injected raise on add_to_vlan"):
        d.add_to_vlan(port="eth1", vlan_id=5, tag="tagged")
