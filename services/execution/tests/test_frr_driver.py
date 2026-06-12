"""Unit tests for the FRR Management driver (drivers/frr_mgmt/driver.py).

The driver is exercised directly with netmiko's ConnectHandler mocked, so these
run with no network and no sandbox subprocess. They pin the behaviors that
matter: dry-run must never open a connection (the binding supports_dry_run
claim), configure must feed the right lines to send_config_set and persist, bad
input is rejected, and status degrades to unreachable instead of raising.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the driver module straight from the in-repo driver package.
_DRIVER_PATH = Path(__file__).resolve().parents[3] / "drivers" / "frr_mgmt" / "driver.py"
_spec = importlib.util.spec_from_file_location("frr_mgmt_driver", _DRIVER_PATH)
frr_driver = importlib.util.module_from_spec(_spec)
sys.modules["frr_mgmt_driver"] = frr_driver
_spec.loader.exec_module(frr_driver)

Driver = frr_driver.Driver
DriverError = frr_driver.DriverError

_REAL_CTX = {
    "HERD_ip": "10.99.0.11",
    "HERD_login": "netadmin",
    "HERD_password": "demo123",
}
_DRY_CTX = {**_REAL_CTX, "dry_run": True}


# --- dry-run must never touch the wire --------------------------------------


def test_dry_run_configure_does_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        result = d.configure(commands=["ip route 192.0.2.0/24 blackhole"])
    ch.assert_not_called()  # the binding supports_dry_run guarantee
    assert result["simulated"] is True
    assert result["applied"] == ["ip route 192.0.2.0/24 blackhole"]


def test_dry_run_status_does_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        result = d.status()
    ch.assert_not_called()
    assert result["reachable"] is True
    assert result["simulated"] is True


def test_dry_run_login_logout_do_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        assert d.login()["simulated"] is True
        assert d.logout()["simulated"] is True
    ch.assert_not_called()


# --- real configure feeds the right commands and persists -------------------


def test_configure_sends_commands_and_saves():
    conn = MagicMock()
    conn.send_config_set.return_value = "config applied"
    conn.save_config.return_value = "ok"
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        d = Driver(_REAL_CTX)
        result = d.configure(
            commands=["ip route 10.0.0.0/8 blackhole", "ip route 11.0.0.0/8 blackhole"]
        )
    # connected with the cisco_ios platform to the device's HERD_ip
    _, kwargs = ch.call_args
    assert kwargs["device_type"] == "cisco_ios"
    assert kwargs["host"] == "10.99.0.11"
    assert kwargs["username"] == "netadmin"
    # the exact config lines went to send_config_set, and the change was persisted
    conn.send_config_set.assert_called_once_with(
        ["ip route 10.0.0.0/8 blackhole", "ip route 11.0.0.0/8 blackhole"]
    )
    conn.save_config.assert_called_once()
    assert result["success"] is True


def test_configure_accepts_single_command_string():
    conn = MagicMock()
    conn.send_config_set.return_value = ""
    conn.save_config.return_value = ""
    with patch("netmiko.ConnectHandler", return_value=conn):
        Driver(_REAL_CTX).configure(command="ip route 192.0.2.0/24 blackhole")
    conn.send_config_set.assert_called_once_with(["ip route 192.0.2.0/24 blackhole"])


def test_configure_without_commands_raises():
    with patch("netmiko.ConnectHandler"):
        d = Driver(_REAL_CTX)
        with pytest.raises(DriverError):
            d.configure()


# --- status degrades gracefully, login validates params ---------------------


def test_status_reports_unreachable_on_connect_failure():
    with patch("netmiko.ConnectHandler", side_effect=OSError("no route to host")):
        d = Driver(_REAL_CTX)
        result = d.status()
    assert result["reachable"] is False
    assert "no route to host" in result["error"]


def test_status_parses_version_banner():
    conn = MagicMock()
    conn.send_command.return_value = "FRRouting 8.4_git (r1) on Linux"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).status()
    assert result["reachable"] is True
    assert result["version"].startswith("FRRouting")


def test_login_requires_connection_params():
    d = Driver({"dry_run": False})  # no HERD_ip / HERD_login
    with pytest.raises(DriverError):
        d.login()


# --- published config schema (issue #23): lets configure accept raw vtysh -----


def test_config_schema_is_a_valid_draft_2020_12_schema():
    import jsonschema

    schema = Driver.config_schema()
    # Must not raise: the execution sanitizer pins published schemas to 2020-12.
    jsonschema.Draft202012Validator.check_schema(schema)


def test_config_schema_is_ssrf_safe_no_ref_or_id():
    """The schema must carry no base-URI or remote-reference keywords, so the
    execution-side sanitizer accepts it without stripping or rejecting it."""
    import json

    blob = json.dumps(Driver.config_schema())
    for forbidden in ("$ref", "$id", "$schema", "$anchor", "$dynamicRef"):
        assert forbidden not in blob


def test_config_schema_accepts_commands_and_command():
    import jsonschema

    schema = Driver.config_schema()
    jsonschema.validate({"commands": ["ip route 192.0.2.0/24 blackhole"]}, schema)
    jsonschema.validate({"command": "hostname r1-demo"}, schema)


def test_config_schema_rejects_bad_shapes():
    import jsonschema

    schema = Driver.config_schema()
    # non-string command line
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"commands": [123]}, schema)
    # empty list (minItems: 1)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"commands": []}, schema)
    # additional property outside the driver's vocabulary
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"vlan": 10}, schema)
