"""Unit tests for the Nokia SR Linux Layer 2 Switch driver (drivers/srl_l2/driver.py).

The driver is exercised directly with netmiko's ConnectHandler mocked, so
these run with no network and no sandbox subprocess. They pin the behaviors
that matter: the exact absolute-path command text for create/add(tagged)/
add(untagged)/remove/delete, that a commit is issued for every mutating
operation (a candidate change that is never committed never lands), dry-run
must never open a connection (the binding supports_dry_run claim), the
documented return shapes, and DriverError on missing connection params.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "drivers" / "srl_l2"
_DRIVER_PATH = _DRIVER_DIR / "driver.py"

_spec = importlib.util.spec_from_file_location("srl_l2_driver", _DRIVER_PATH)
srl_driver = importlib.util.module_from_spec(_spec)
sys.modules["srl_l2_driver"] = srl_driver
_spec.loader.exec_module(srl_driver)

Driver = srl_driver.Driver
DriverError = srl_driver.DriverError

_REAL_CTX = {
    "HERD_ip": "127.0.0.1",
    "HERD_login": "admin",
    "HERD_password": "NokiaSrl1!",
    "HERD_port": 2223,
}
_DRY_CTX = {**_REAL_CTX, "dry_run": True}

# The execution loader (driver_loader.validate_driver) requires exactly these
# methods for a "Layer 2 Switch" driver; pin them here so a rename breaks in
# this fast unit test rather than only in a stack-only integration run.
L2_REQUIRED_METHODS = (
    "login",
    "logout",
    "create_vlan",
    "add_to_vlan",
    "remove_from_vlan",
    "delete_vlan",
    "status",
)


def test_exposes_the_l2_method_set():
    d = Driver({})
    for name in L2_REQUIRED_METHODS:
        assert callable(getattr(d, name)), f"missing required L2 method {name}"


def test_metadata_declares_l2_and_dry_run():
    meta = json.loads((_DRIVER_DIR / "driver_metadata.json").read_text())
    assert meta["connection_type"] == "Layer 2 Switch"
    assert meta["supports_dry_run"] is True


# --- exact command text: create / add tagged / add untagged / remove / delete ---


def test_create_vlan_sends_exact_command_and_commits():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "All changes have been committed."
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).create_vlan(100)
    conn.send_config_set.assert_called_once_with(["set / network-instance vlan100 type mac-vrf"])
    conn.commit.assert_called_once()
    assert result == {"success": True}


def test_add_to_vlan_tagged_sends_exact_commands():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "All changes have been committed."
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/1", vlan_id=100, tag="tagged")
    conn.send_config_set.assert_called_once_with(
        [
            "set / interface ethernet-1/1 vlan-tagging true",
            "set / interface ethernet-1/1 subinterface 100 type bridged",
            "set / interface ethernet-1/1 subinterface 100 vlan encap single-tagged vlan-id 100",
            "set / network-instance vlan100 interface ethernet-1/1.100",
        ]
    )
    conn.commit.assert_called_once()
    assert result == {"success": True}


def test_add_to_vlan_default_tag_is_tagged():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "All changes have been committed."
    with patch("netmiko.ConnectHandler", return_value=conn):
        Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/1", vlan_id=100)
    sent = conn.send_config_set.call_args[0][0]
    assert (
        "set / interface ethernet-1/1 subinterface 100 vlan encap single-tagged vlan-id 100" in sent
    )


def test_add_to_vlan_untagged_sends_exact_commands():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "All changes have been committed."
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/2", vlan_id=200, tag="untagged")
    conn.send_config_set.assert_called_once_with(
        [
            "set / interface ethernet-1/2 vlan-tagging true",
            "set / interface ethernet-1/2 subinterface 200 type bridged",
            "set / interface ethernet-1/2 subinterface 200 vlan encap untagged",
            "set / network-instance vlan200 interface ethernet-1/2.200",
        ]
    )
    conn.commit.assert_called_once()
    assert result == {"success": True}


def test_remove_from_vlan_sends_exact_commands_and_leaves_vlan_tagging_alone():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "All changes have been committed."
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_from_vlan(port="ethernet-1/1", vlan_id=100)
    conn.send_config_set.assert_called_once_with(
        [
            "delete / network-instance vlan100 interface ethernet-1/1.100",
            "delete / interface ethernet-1/1 subinterface 100",
        ]
    )
    conn.commit.assert_called_once()
    assert result == {"success": True}
    # Deliberately does not touch vlan-tagging: another VLAN may still use the port.
    sent = conn.send_config_set.call_args[0][0]
    assert not any("vlan-tagging" in line for line in sent)


def test_delete_vlan_sends_exact_command_and_commits():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "Nothing to commit."
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).delete_vlan(100)
    conn.send_config_set.assert_called_once_with(["delete / network-instance vlan100"])
    conn.commit.assert_called_once()
    assert result == {"success": True}


# --- commit is issued for every mutating operation, one session is reused ------


def test_commit_is_issued_for_every_mutating_operation_over_one_shared_session():
    conn = MagicMock()
    conn.send_config_set.return_value = "(ok)"
    conn.commit.return_value = "All changes have been committed."
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        d = Driver(_REAL_CTX)
        d.login()
        d.create_vlan(100)
        d.add_to_vlan(port="ethernet-1/1", vlan_id=100, tag="tagged")
        d.remove_from_vlan(port="ethernet-1/1", vlan_id=100)
        d.delete_vlan(100)
        d.logout()
    # One session for the whole batch (login connects once; later calls reuse it).
    ch.assert_called_once()
    conn.disconnect.assert_called_once()
    # login/logout do not touch candidate config; the four VLAN ops each commit once.
    assert conn.commit.call_count == 4


# --- dry-run must never touch the wire --------------------------------------


def test_dry_run_methods_never_open_a_session():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        assert d.login()["simulated"] is True
        assert d.create_vlan(100)["simulated"] is True
        assert d.add_to_vlan(port="ethernet-1/1", vlan_id=100, tag="tagged")["simulated"] is True
        assert d.add_to_vlan(port="ethernet-1/2", vlan_id=200, tag="untagged")["simulated"] is True
        assert d.remove_from_vlan(port="ethernet-1/1", vlan_id=100)["simulated"] is True
        assert d.delete_vlan(100)["simulated"] is True
        assert d.status()["simulated"] is True
        assert d.logout()["simulated"] is True
    ch.assert_not_called()  # the binding supports_dry_run guarantee


def test_dry_run_create_vlan_records_the_command_it_would_have_sent():
    recorded = []
    with patch("netmiko.ConnectHandler"):
        with patch.object(
            srl_driver, "record_command", side_effect=lambda *a, **k: recorded.append((a, k))
        ):
            Driver(_DRY_CTX).create_vlan(100)
    commands = [args[0] for args, _ in recorded]
    assert "set / network-instance vlan100 type mac-vrf" in commands
    for _args, kwargs in recorded:
        assert kwargs.get("exit_status") == "simulated"


# --- return shapes match the contract ---------------------------------------


def test_return_shapes_match_the_contract_in_dry_run():
    d = Driver(_DRY_CTX)
    with patch("netmiko.ConnectHandler"):
        for method, kwargs in (
            (d.login, {}),
            (d.create_vlan, {"vlan_id": 100}),
            (d.add_to_vlan, {"port": "ethernet-1/1", "vlan_id": 100}),
            (d.remove_from_vlan, {"port": "ethernet-1/1", "vlan_id": 100}),
            (d.delete_vlan, {"vlan_id": 100}),
            (d.logout, {}),
        ):
            result = method(**kwargs)
            assert isinstance(result.get("success"), bool)
        status = d.status()
        assert isinstance(status.get("reachable"), bool)


# --- status ------------------------------------------------------------------


def test_status_reports_reachable_when_sr_linux_in_version_banner():
    conn = MagicMock()
    conn.send_command.return_value = "OS : SR Linux\nSoftware Version : v26.7.2"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).status()
    assert result == {"reachable": True}


def test_status_reports_unreachable_on_connect_failure():
    with patch("netmiko.ConnectHandler", side_effect=OSError("no route to host")):
        result = Driver(_REAL_CTX).status()
    assert result["reachable"] is False
    assert "no route to host" in result["error"]


# --- DriverError on missing connection params, matching frr_mgmt -------------


def test_login_requires_connection_params():
    d = Driver({"dry_run": False})  # no HERD_ip / HERD_login
    with pytest.raises(DriverError):
        d.login()


def test_dry_run_login_does_not_require_connection_params():
    d = Driver({"dry_run": True})  # no HERD_ip / HERD_login, but dry-run
    assert d.login()["simulated"] is True
