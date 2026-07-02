"""Unit tests for the checked-in mock Layer 3 switch driver.

Pure, stack-free: load drivers/mock_l3/driver.py by path and exercise the Driver
class directly. Guards the contract the execution sandbox depends on (the L3
method set, return shapes, dry-run, and the failure-injection knobs the route
integration tests rely on) without needing Docker.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "drivers" / "mock_l3"
_DRIVER_PATH = _DRIVER_DIR / "driver.py"

# The execution loader (driver_loader.validate_driver) requires exactly these
# methods for a "Layer 3 Switch" driver; pin them here so a rename breaks in this
# fast unit test rather than only in a stack-only integration run.
L3_REQUIRED_METHODS = (
    "login",
    "logout",
    "configure_route",
    "remove_route",
    "status",
)


@pytest.fixture(scope="module")
def driver_cls():
    spec = importlib.util.spec_from_file_location("mock_l3_driver", _DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver


def test_exposes_the_l3_method_set(driver_cls):
    d = driver_cls({})
    for name in L3_REQUIRED_METHODS:
        assert callable(getattr(d, name)), f"missing required L3 method {name}"
    # Optional extra beyond the contract, kept so config-apply jobs stay demonstrable.
    assert callable(d.configure)


def test_metadata_declares_l3_and_dry_run():
    meta = json.loads((_DRIVER_DIR / "driver_metadata.json").read_text())
    assert meta["connection_type"] == "Layer 3 Switch"
    assert meta["supports_dry_run"] is True


def test_route_ops_return_success_and_echo_kwargs(driver_cls):
    d = driver_cls({})
    assert d.login() == {"success": True, "output": {}}
    assert d.configure_route(
        destination="10.0.0.0/24", next_hop="192.168.1.1", interface="eth0"
    ) == {
        "success": True,
        "output": {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    }
    assert d.remove_route(destination="10.0.0.0/24", next_hop="192.168.1.1", interface="eth0") == {
        "success": True,
        "output": {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    }
    assert d.logout() == {"success": True, "output": {}}


def test_configure_route_accepts_interface_route(driver_cls):
    # next_hop None is an interface route; it still succeeds and echoes the None.
    res = driver_cls({}).configure_route(destination="10.0.0.0/24", next_hop=None, interface="eth0")
    assert res["success"] is True
    assert res["output"] == {"destination": "10.0.0.0/24", "next_hop": None, "interface": "eth0"}


def test_configure_acknowledges_config(driver_cls):
    res = driver_cls({}).configure(routes=[{"destination": "10.0.0.0/24"}])
    assert res == {
        "success": True,
        "output": {"config": {"routes": [{"destination": "10.0.0.0/24"}]}},
    }


def test_dry_run_flags_simulated(driver_cls):
    res = driver_cls({"dry_run": True}).configure_route(
        destination="10.0.0.0/24", next_hop="192.168.1.1", interface="eth0"
    )
    assert res["success"] is True
    assert res["simulated"] is True
    assert res["output"] == {
        "destination": "10.0.0.0/24",
        "next_hop": "192.168.1.1",
        "interface": "eth0",
    }


def test_status_reports_reachable_and_never_raises(driver_cls):
    assert driver_cls({}).status() == {"reachable": True, "simulated": False}
    # Even with failure injection, status answers rather than raising.
    assert driver_cls({"HERD_mock_fail_actions": "status"}).status()["reachable"] is False
    # Raise injection is ignored on status; it must always answer.
    assert driver_cls({"HERD_mock_raise_actions": "status"}).status()["reachable"] is True


def test_fail_injection_returns_unsuccessful_result(driver_cls):
    d = driver_cls({"HERD_mock_fail_actions": "configure_route,configure"})
    fail = d.configure_route(destination="10.0.0.0/24", next_hop="192.168.1.1", interface="eth0")
    assert fail["success"] is False
    assert "injected" in fail["error"]
    assert d.configure(hostname="r1")["success"] is False
    # An action not in the injected set still succeeds.
    assert (
        d.remove_route(destination="10.0.0.0/24", next_hop="192.168.1.1", interface="eth0")[
            "success"
        ]
        is True
    )


def test_raise_injection_raises(driver_cls):
    d = driver_cls({"HERD_mock_raise_actions": "remove_route"})
    with pytest.raises(RuntimeError, match="injected raise on remove_route"):
        d.remove_route(destination="10.0.0.0/24", next_hop="192.168.1.1", interface="eth0")
