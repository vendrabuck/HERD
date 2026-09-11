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


# --- device rejection must be reported as failure, not success (issue #771) --


def test_configure_rejected_by_device_reports_failure_with_offending_line():
    """A vtysh '%' line in the output is a genuine rejection: configure()
    must return success: False with the offending line surfaced, and must
    NOT save the (unapplied) config. Before issue #771 this driver never
    inspected output and always reported success here."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        "frr(config)#  ip route 999.999.999.0/24 172.17.0.1\n"
        "% Unknown command: ip route 999.999.999.0/24 172.17.0.1\n"
        "frr(config)#  end\n"
        "frr# "
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.configure(commands=["ip route 999.999.999.0/24 172.17.0.1"])
    assert result["success"] is False
    assert result["error"] == "% Unknown command: ip route 999.999.999.0/24 172.17.0.1"
    assert "output" in result
    conn.save_config.assert_not_called()


def test_configure_clean_apply_still_succeeds():
    """A clean apply (no '%' line) must still report success and persist,
    unchanged by the new error-detection path."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        "frr(config)#  ip route 192.0.2.0/24 blackhole\n"
        "frr(config)#  end\n"
        "frr# "
    )
    conn.save_config.return_value = "ok"
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.configure(commands=["ip route 192.0.2.0/24 blackhole"])
    assert result["success"] is True
    assert "error" not in result
    conn.save_config.assert_called_once()


def test_configure_carves_out_the_benign_remove_route_line_as_success():
    """Lane's ruling (reversing the earlier no-carve-out decision): FRR's
    "already absent" response to removing a route means the desired end
    state (the route is gone) already holds, so configure() must report
    success here, the same as drivers/frr_l3's remove_route does for the
    identical device text. The benign line is still surfaced, under
    "benign_warnings" rather than "error", so an operator can see it."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        "frr(config)#  no ip route 203.0.113.0/30 172.17.0.1\n"
        "% Refusing to remove a non-existent route\n"
        "frr(config)#  end\n"
        "frr# "
    )
    conn.save_config.return_value = "ok"
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.configure(commands=["no ip route 203.0.113.0/30 172.17.0.1"])
    assert result["success"] is True
    assert "error" not in result
    assert result["benign_warnings"] == ["% Refusing to remove a non-existent route"]
    conn.save_config.assert_called_once()


def test_configure_reports_genuine_failure_even_after_a_benign_line():
    """The masking bug this whole ruling is about: if configure() only
    inspected the FIRST '%' line, a benign line arriving before a genuine
    one would hide the genuine failure entirely. This batch's first error is
    the benign 'already absent' marker; its second is a real rejection, and
    the result must be failure, reporting the FIRST GENUINE line, not the
    benign one that came first in the output."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        "frr(config)#  no ip route 203.0.113.0/30 172.17.0.1\n"
        "% Refusing to remove a non-existent route\n"
        "frr(config)#  ip route 999.999.999.0/24 172.17.0.1\n"
        "% Unknown command: ip route 999.999.999.0/24 172.17.0.1\n"
        "frr(config)#  end\n"
        "frr# "
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.configure(
            commands=[
                "no ip route 203.0.113.0/30 172.17.0.1",
                "ip route 999.999.999.0/24 172.17.0.1",
            ]
        )
    assert result["success"] is False
    assert result["error"] == "% Unknown command: ip route 999.999.999.0/24 172.17.0.1"
    conn.save_config.assert_not_called()


def test_configure_reports_genuine_failure_when_it_comes_first():
    """The symmetric ordering, for completeness: a genuine failure followed
    by a benign line must still report the genuine one."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        "frr(config)#  ip route 999.999.999.0/24 172.17.0.1\n"
        "% Unknown command: ip route 999.999.999.0/24 172.17.0.1\n"
        "frr(config)#  no ip route 203.0.113.0/30 172.17.0.1\n"
        "% Refusing to remove a non-existent route\n"
        "frr(config)#  end\n"
        "frr# "
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.configure(
            commands=[
                "ip route 999.999.999.0/24 172.17.0.1",
                "no ip route 203.0.113.0/30 172.17.0.1",
            ]
        )
    assert result["success"] is False
    assert result["error"] == "% Unknown command: ip route 999.999.999.0/24 172.17.0.1"
    conn.save_config.assert_not_called()


def test_dry_run_configure_rejected_command_is_not_evaluated():
    """A dry-run never opens a connection, so error detection never runs;
    the recorded transcript is what a user reviews, not a driver verdict."""
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        result = d.configure(commands=["ip route 999.999.999.0/24 172.17.0.1"])
    ch.assert_not_called()
    assert result["success"] is True
    assert result["simulated"] is True


def test_backup_rejected_by_device_reports_failure():
    conn = MagicMock()
    conn.send_command.return_value = "% Unknown command: show running-config"
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.backup()
    assert result["success"] is False
    assert result["error"] == "% Unknown command: show running-config"


def test_backup_clean_read_still_succeeds():
    conn = MagicMock()
    conn.send_command.return_value = "Building configuration...\n!\nhostname r1\n!\nend"
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        result = d.backup()
    assert result["success"] is True
    assert result["config"] == conn.send_command.return_value


def test_dry_run_backup_does_not_evaluate_output():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        result = d.backup()
    ch.assert_not_called()
    assert result["success"] is True
    assert result["simulated"] is True
    assert result["config"] is None


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
