"""Unit tests for the FRR Layer 3 Switch driver (drivers/frr_l3/driver.py).

Pure, stack-free: load the driver module by path and exercise the Driver class
directly with netmiko's ConnectHandler mocked (matching
services/execution/tests/test_frr_driver.py's house style for drivers/frr_mgmt).
Guards the contract the execution sandbox depends on (the L3 method set, exact
vtysh command text for both route forms, dry-run gating, and the documented
return shapes) without needing hardware; tests/nos_lab/test_frr_l3_driver_live.py
is the live counterpart against the real NOS test lab node.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "drivers" / "frr_l3"
_DRIVER_PATH = _DRIVER_DIR / "driver.py"

_spec = importlib.util.spec_from_file_location("frr_l3_driver", _DRIVER_PATH)
frr_l3_driver = importlib.util.module_from_spec(_spec)
sys.modules["frr_l3_driver"] = frr_l3_driver
_spec.loader.exec_module(frr_l3_driver)

Driver = frr_l3_driver.Driver
DriverError = frr_l3_driver.DriverError

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

_REAL_CTX = {
    "HERD_ip": "127.0.0.1",
    "HERD_login": "netadmin",
    "HERD_password": "netadmin",
    "HERD_port": 2224,
}
_DRY_CTX = {**_REAL_CTX, "dry_run": True}


def test_exposes_the_l3_method_set():
    d = Driver(_REAL_CTX)
    for name in L3_REQUIRED_METHODS:
        assert callable(getattr(d, name)), f"missing required L3 method {name}"


def test_metadata_declares_l3_and_dry_run():
    meta = json.loads((_DRIVER_DIR / "driver_metadata.json").read_text())
    assert meta["connection_type"] == "Layer 3 Switch"
    assert meta["supports_dry_run"] is True


# --- exact command text for both route forms --------------------------------


def test_configure_route_with_next_hop_sends_exact_command():
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)# ip route 192.0.2.0/30 172.20.255.254"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    conn.send_config_set.assert_called_once_with(["ip route 192.0.2.0/30 172.20.255.254"])
    assert result["success"] is True


def test_configure_route_interface_route_omits_next_hop():
    # next_hop=None is an interface route: "ip route <dest> <interface>", no next-hop.
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)# ip route 192.0.2.4/30 eth0"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure_route(
            destination="192.0.2.4/30", next_hop=None, interface="eth0"
        )
    conn.send_config_set.assert_called_once_with(["ip route 192.0.2.4/30 eth0"])
    assert result["success"] is True


def test_remove_route_with_next_hop_sends_exact_no_command():
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)# no ip route 192.0.2.0/30 172.20.255.254"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    conn.send_config_set.assert_called_once_with(["no ip route 192.0.2.0/30 172.20.255.254"])
    assert result["success"] is True


def test_remove_route_interface_route_omits_next_hop():
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)# no ip route 192.0.2.4/30 eth0"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="192.0.2.4/30", next_hop=None, interface="eth0"
        )
    conn.send_config_set.assert_called_once_with(["no ip route 192.0.2.4/30 eth0"])
    assert result["success"] is True


def test_remove_route_of_an_already_removed_route_still_reports_success():
    # Verified live: FRR answers "% Refusing to remove a non-existent route" for
    # this case but netmiko does not raise. This is the ONE "%" line the driver
    # deliberately treats as success rather than failure: the desired end state
    # (the route is gone) already holds. This is the idempotency decision from
    # the module docstring / PR report, pinned here so a future change cannot
    # regress it silently.
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)# no ip route 192.0.2.0/30 172.20.255.254\n"
        "% Refusing to remove a non-existent route"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert result["success"] is True
    assert result["already_absent"] is True


# --- a "%" line is a genuine rejection, not a success (the blocking bug fix) -


def test_configure_route_rejected_by_device_reports_failure():
    # Regression test: a route the device REJECTS (a malformed destination)
    # must not be reported as success. Verified live: vtysh answers
    # "% Unknown command: ..." and netmiko does not raise, so the driver must
    # scan the output itself rather than trust the absence of an exception.
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  ip route 999.999.999.0/24 172.17.0.1\n"
        "% Unknown command: ip route 999.999.999.0/24 172.17.0.1\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure_route(
            destination="999.999.999.0/24", next_hop="172.17.0.1", interface="eth0"
        )
    assert result["success"] is False
    assert "Unknown command" in result["error"]


def test_remove_route_rejected_by_device_reports_failure():
    # A genuine rejection on remove_route (not the benign "already absent"
    # case) must also report failure, not success.
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  no ip route not-a-prefix 172.17.0.1\n"
        "% Unknown command: no ip route not-a-prefix 172.17.0.1\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="not-a-prefix", next_hop="172.17.0.1", interface="eth0"
        )
    assert result["success"] is False
    assert "Unknown command" in result["error"]
    assert "already_absent" not in result


def test_configure_route_clean_apply_still_reports_success():
    # A clean apply (no "%" line anywhere in the output) is unaffected by the
    # error-detection change.
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)#  ip route 192.0.2.0/30 172.20.255.254"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert result["success"] is True
    assert "error" not in result


def test_configure_route_of_an_already_configured_route_is_a_silent_no_op():
    # Verified live: re-sending an already-configured route produces the exact
    # same output as the first apply, no error. The driver needs no special
    # handling for this direction; FRR itself is idempotent.
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)# ip route 192.0.2.0/30 172.20.255.254"
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        first = d.configure_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
        second = d.configure_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert first["success"] is True
    assert second["success"] is True


# --- the benign carve-out is anchored, and every line is classified ---------
# (issue #779: the same two defects drivers/frr_mgmt carried.)


def test_remove_route_echoed_benign_marker_is_a_genuine_failure():
    """vtysh echoes the offending command back inside its own rejection, so a
    command whose text CONTAINS the benign phrase yields a genuine
    "% Unknown command" line that also contains it. Verified live 2026-09-12
    against the NOS test lab FRR node:

        $ docker exec nos-test-frr vtysh -c "configure terminal" \
              -c "ip route Refusing to remove a non-existent route"
        % Unknown command: ip route Refusing to remove a non-existent route

    Cabling validates this driver's destination/next-hop as IP objects, so
    HERD's own call path cannot reach this today; the anchored test is what
    keeps that a defense in depth rather than a dependency on a caller three
    services away."""
    destination = "Refusing to remove a non-existent route"
    conn = MagicMock()
    conn.send_config_set.return_value = (
        f"frr(config)#  no ip route {destination} 172.17.0.1\n"
        f"% Unknown command: no ip route {destination} 172.17.0.1\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination=destination, next_hop="172.17.0.1", interface="eth0"
        )
    assert result["success"] is False, (
        f"an echoed benign phrase inside a genuine rejection must not be carved out: {result!r}"
    )
    assert "Unknown command" in result["error"]
    assert "already_absent" not in result


def test_remove_route_genuine_line_after_a_benign_one_is_not_masked():
    """A first-match scanner would stop at the benign "already absent" line
    and report success, hiding the genuine rejection printed after it
    (docs/DRIVERS.md, "Classify ALL of a device's complaints before
    deciding")."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  no ip route 192.0.2.0/30 172.20.255.254\n"
        "% Refusing to remove a non-existent route\n"
        "% Unknown command: no ip route 192.0.2.0/30 172.20.255.254\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert result["success"] is False
    assert "Unknown command" in result["error"]
    assert "already_absent" not in result


def test_remove_route_genuine_line_before_a_benign_one_is_reported():
    """The symmetric ordering: the FIRST GENUINE line is the error, not the
    first line of any kind."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  no ip route 192.0.2.0/30 172.20.255.254\n"
        "% Unknown command: no ip route 192.0.2.0/30 172.20.255.254\n"
        "% Refusing to remove a non-existent route\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert result["success"] is False
    assert result["error"] == "% Unknown command: no ip route 192.0.2.0/30 172.20.255.254"


def test_remove_route_all_benign_lines_still_report_already_absent():
    """The carve-out itself is unchanged when every line found is benign,
    including a response that repeats the marker."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  no ip route 192.0.2.0/30 172.20.255.254\n"
        "% Refusing to remove a non-existent route\n"
        "% Refusing to remove a non-existent route\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert result["success"] is True
    assert result["already_absent"] is True


def test_configure_route_does_not_carve_out_the_removal_marker():
    """The benign line belongs to remove_route alone: an install that somehow
    drew it is a rejection, not an idempotent no-op."""
    conn = MagicMock()
    conn.send_config_set.return_value = (
        "frr(config)#  ip route 192.0.2.0/30 172.20.255.254\n"
        "% Refusing to remove a non-existent route\n"
        "frr(config)#  end"
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).configure_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    assert result["success"] is False


def test_driver_never_calls_save_config():
    """Deliberate asymmetry with drivers/frr_mgmt (module docstring and
    docs/DRIVERS.md): these routes are reservation-scoped, so they live in the
    running config and are never written to the startup config."""
    conn = MagicMock()
    conn.send_config_set.return_value = "frr(config)# ip route 192.0.2.0/30 172.20.255.254"
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        d.configure_route(destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0")
        d.remove_route(destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0")
    conn.save_config.assert_not_called()


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
    ctx = {k: v for k, v in _REAL_CTX.items() if k != "HERD_port"}
    conn = MagicMock()
    conn.send_command.return_value = "FRRouting 8.4_git (r1) on Linux"
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        Driver(ctx).status()
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
            d.configure_route(
                destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
            )
    ch.assert_not_called()


def test_non_integer_port_degrades_status_to_unreachable():
    """status() must never raise, whatever the field_data says."""
    with patch("netmiko.ConnectHandler"):
        result = Driver({**_REAL_CTX, "HERD_port": "abc"}).status()
    assert result["reachable"] is False
    assert "HERD_port must be an integer" in result["error"]


# --- dry-run must never touch the wire --------------------------------------


def test_dry_run_configure_route_does_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        result = Driver(_DRY_CTX).configure_route(
            destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
        )
    ch.assert_not_called()  # the binding supports_dry_run guarantee
    assert result["success"] is True
    assert result["simulated"] is True


def test_dry_run_remove_route_does_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        result = Driver(_DRY_CTX).remove_route(
            destination="192.0.2.0/30", next_hop=None, interface="eth0"
        )
    ch.assert_not_called()
    assert result["success"] is True
    assert result["simulated"] is True


def test_dry_run_login_logout_do_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        d = Driver(_DRY_CTX)
        assert d.login() == {"success": True, "simulated": True}
        assert d.logout() == {"success": True, "simulated": True}
    ch.assert_not_called()


def test_dry_run_status_does_not_connect():
    with patch("netmiko.ConnectHandler") as ch:
        result = Driver(_DRY_CTX).status()
    ch.assert_not_called()
    assert result == {"reachable": True, "simulated": True}


def test_dry_run_records_transcript_and_marks_simulated():
    calls = []
    with patch.object(
        frr_l3_driver, "record_command", side_effect=lambda *a, **k: calls.append((a, k))
    ):
        with patch("netmiko.ConnectHandler") as ch:
            Driver(_DRY_CTX).configure_route(
                destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0"
            )
    ch.assert_not_called()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "ip route 192.0.2.0/30 172.20.255.254"
    assert kwargs["exit_status"] == "simulated"


# --- documented return shapes ------------------------------------------------


def test_login_returns_documented_shape():
    with patch("netmiko.ConnectHandler", return_value=MagicMock()):
        assert Driver(_REAL_CTX).login() == {"success": True}


def test_logout_returns_documented_shape():
    conn = MagicMock()
    with patch("netmiko.ConnectHandler", return_value=conn):
        d = Driver(_REAL_CTX)
        d.login()
        assert d.logout() == {"success": True}


def test_status_returns_reachable_not_success():
    conn = MagicMock()
    conn.send_command.return_value = "FRRouting 8.4_git (r1) on Linux"
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).status()
    assert result["reachable"] is True
    assert "success" not in result


def test_status_reports_unreachable_on_connect_failure_without_raising():
    with patch("netmiko.ConnectHandler", side_effect=OSError("no route to host")):
        result = Driver(_REAL_CTX).status()
    assert result["reachable"] is False
    assert "no route to host" in result["error"]


# --- DriverError on missing connection params --------------------------------


def test_login_requires_connection_params():
    d = Driver({"dry_run": False})  # no HERD_ip / HERD_login
    with pytest.raises(DriverError):
        d.login()


def test_configure_route_requires_connection_params():
    d = Driver({"dry_run": False})
    with pytest.raises(DriverError):
        d.configure_route(destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0")


def test_remove_route_requires_connection_params():
    d = Driver({"dry_run": False})
    with pytest.raises(DriverError):
        d.remove_route(destination="192.0.2.0/30", next_hop="172.20.255.254", interface="eth0")


def test_status_degrades_gracefully_on_missing_params_rather_than_raising():
    # status() must never raise; missing params surface as unreachable, same as
    # any other connection failure (matches drivers/frr_mgmt's status()).
    d = Driver({"dry_run": False})
    result = d.status()
    assert result["reachable"] is False
    assert "error" in result
