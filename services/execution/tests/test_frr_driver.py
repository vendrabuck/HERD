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

# The real netmiko save_config() output from the live NOS test lab FRR node,
# captured 2026-09-12 (per-daemon form, the shape a node with no integrated
# /etc/frr/frr.conf prints). configure() requires this positive evidence of a
# save, so a mock that returns "ok" is not a stand-in for it any more.
_SAVE_OK = (
    "write mem\n"
    "Note: this version of vtysh never writes vtysh.conf\n"
    "Building Configuration...\n"
    "Configuration saved to /etc/frr/zebra.conf\n"
    "Configuration saved to /etc/frr/staticd.conf\n"
    "frr# "
)

# The same node with an integrated /etc/frr/frr.conf: one line, different
# wording, still a successful save (captured live the same day).
_SAVE_OK_INTEGRATED = (
    "write mem\n"
    "Note: this version of vtysh never writes vtysh.conf\n"
    "Building Configuration...\n"
    "Integrated configuration saved to /etc/frr/frr.conf\n"
    "[OK]\n"
    "frr# "
)

# A save into an unwritable /etc/frr, captured live the same day: no "%"
# line anywhere and vtysh exits 0, which is exactly why configure() cannot
# classify persistence by scanning for rejections (issue #779).
_SAVE_FAILED = (
    "write mem\n"
    "Note: this version of vtysh never writes vtysh.conf\n"
    "Building Configuration...\n"
    "Can't open configuration file /etc/frr/zebra.conf.XXXXXX.\n"
    "Can't open configuration file /etc/frr/staticd.conf.XXXXXX.\n"
    "frr# "
)


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
    conn.save_config.return_value = _SAVE_OK
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
    conn.save_config.return_value = _SAVE_OK
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
    # The batch is not "applied": some prefix of it may have landed, the rest
    # did not (issue #779, item 3).
    assert result["attempted"] == ["ip route 999.999.999.0/24 172.17.0.1"]
    assert "applied" not in result
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
    conn.save_config.return_value = _SAVE_OK
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
    conn.save_config.return_value = _SAVE_OK
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


def test_configure_rejects_an_echoed_benign_marker_as_a_genuine_failure():
    """The substring-carve-out bug (issue #779, item 1). vtysh echoes the
    offending command back inside its own rejection, so a config line that
    merely CONTAINS the benign phrase produces a genuine "% Unknown command"
    line that also contains it. Verified live 2026-09-12:

        $ docker exec nos-test-frr vtysh -c "configure terminal" \
              -c "ip route Refusing to remove a non-existent route"
        % Unknown command: ip route Refusing to remove a non-existent route

    An unanchored `marker in line` test called that benign and reported
    success; the anchored `line.startswith(marker)` test calls it what it is.
    Nothing upstream filters the phrase: the published config schema accepts
    any string up to 512 characters."""
    command = "ip route Refusing to remove a non-existent route"
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        f"frr(config)#  {command}\n"
        f"% Unknown command: {command}\n"
        "frr(config)#  end\n"
        "frr# "
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure(commands=[command])
    assert result["success"] is False, (
        f"an echoed benign phrase inside a genuine rejection must not be carved out: {result!r}"
    )
    assert result["error"] == f"% Unknown command: {command}"
    assert "benign_warnings" not in result
    conn.save_config.assert_not_called()


# --- persistence is classified, not assumed (issue #779, item 2) ------------


def test_configure_reports_failure_when_write_memory_could_not_save():
    """A failed `write memory` prints NO "%" line and vtysh exits 0, so the
    rejection scan cannot see it. The config reached the running config but is
    not persisted, so this is a failure, with the offending save line as the
    error and the commands named "attempted"."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        " configure terminal\n"
        "frr(config)#  ip route 192.0.2.0/24 blackhole\n"
        "frr(config)#  end\n"
        "frr# "
    )
    conn.save_config.return_value = _SAVE_FAILED
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure(commands=["ip route 192.0.2.0/24 blackhole"])
    assert result["success"] is False, f"an unpersisted config is not a success: {result!r}"
    assert result["error"] == "Can't open configuration file /etc/frr/zebra.conf.XXXXXX."
    assert result["save_output"] == _SAVE_FAILED
    assert result["attempted"] == ["ip route 192.0.2.0/24 blackhole"]
    assert "applied" not in result


def test_configure_accepts_the_integrated_save_wording():
    """The same node prints a different (single-line) save confirmation when
    it keeps an integrated /etc/frr/frr.conf. Both wordings are a successful
    save; the check is case-insensitive on "configuration saved to" for
    exactly this reason."""
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)#  ip route 192.0.2.0/24 blackhole\nfrr# "
    conn.save_config.return_value = _SAVE_OK_INTEGRATED
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure(commands=["ip route 192.0.2.0/24 blackhole"])
    assert result["success"] is True
    assert "error" not in result


def test_configure_reports_failure_when_the_save_says_nothing_at_all():
    """Positive evidence is required: an empty (or unrecognized) save output
    proves nothing was written, so it cannot be read as success."""
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)#  ip route 192.0.2.0/24 blackhole\nfrr# "
    conn.save_config.return_value = ""
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure(commands=["ip route 192.0.2.0/24 blackhole"])
    assert result["success"] is False
    assert result["error"] == "write memory reported no saved configuration file"


def test_configure_benign_warnings_survive_a_save_failure():
    """A benign "%" line is still operator-visible when the save is what
    failed: the two classifications are independent."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  no ip route 203.0.113.0/30 172.17.0.1\n"
        "% Refusing to remove a non-existent route\n"
        "frr# "
    )
    conn.save_config.return_value = _SAVE_FAILED
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure(commands=["no ip route 203.0.113.0/30 172.17.0.1"])
    assert result["success"] is False
    assert result["benign_warnings"] == ["% Refusing to remove a non-existent route"]


# --- HERD_port parsing (issue #780) -----------------------------------------


def test_blank_port_defaults_to_22_instead_of_raising():
    """A present-but-blank optional `port` device field reaches the driver as
    "", and int("") used to raise inside __init__, before any method could
    run (issue #780)."""
    conn = MagicMock()
    conn.send_command.return_value = "FRRouting 8.4_git (r1) on Linux"
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        result = Driver({**_REAL_CTX, "HERD_port": ""}).status()
    assert result["reachable"] is True
    _, kwargs = ch.call_args
    assert kwargs["port"] == 22


def test_missing_port_defaults_to_22():
    conn = MagicMock()
    conn.send_command.return_value = "FRRouting 8.4_git (r1) on Linux"
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        Driver(_REAL_CTX).status()
    _, kwargs = ch.call_args
    assert kwargs["port"] == 22


def test_numeric_string_port_is_honored():
    conn = MagicMock()
    conn.send_command.return_value = "FRRouting 8.4_git (r1) on Linux"
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        Driver({**_REAL_CTX, "HERD_port": "2224"}).status()
    _, kwargs = ch.call_args
    assert kwargs["port"] == 2224


def test_non_integer_port_does_not_raise_from_the_constructor():
    with patch("netmiko.ConnectHandler"):
        Driver({**_REAL_CTX, "HERD_port": "abc"})  # must not raise


def test_non_integer_port_raises_driver_error_from_the_mutating_path():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver({**_REAL_CTX, "HERD_port": "abc"})
        with pytest.raises(DriverError, match="HERD_port must be an integer"):
            d.configure(commands=["ip route 192.0.2.0/24 blackhole"])
    ch.assert_not_called()


def test_non_integer_port_degrades_status_to_unreachable():
    """status() must never raise, whatever the field_data says."""
    with patch("netmiko.ConnectHandler"):
        result = Driver({**_REAL_CTX, "HERD_port": "abc"}).status()
    assert result["reachable"] is False
    assert "HERD_port must be an integer" in result["error"]


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
