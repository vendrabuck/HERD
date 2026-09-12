"""Unit tests for the Nokia SR Linux Layer 2 Switch driver (drivers/srl_l2/driver.py).

The driver is exercised directly with netmiko's ConnectHandler mocked, so
these run with no network and no sandbox subprocess. They pin the behaviors
that matter: the exact absolute-path command text for create/add(tagged)/
add(untagged)/remove/delete, that a commit is issued for every mutating
operation (a candidate change that is never committed never lands), dry-run
must never open a connection (the binding supports_dry_run claim), the
documented return shapes, DriverError on missing connection params, the
optional HERD_port parsing rules (issue #780), and the candidate discipline
of issue #778: discard at entry, no commit after a set-time rejection,
discard on the exception path.

The connection mock is MagicMock(spec=NokiaSrlSSH), netmiko's real class for
this platform, on purpose. _discard and config_mode are netmiko private/
internal API; a spec-free MagicMock would happily answer to a renamed or
deleted attribute and these tests would keep passing while the driver
silently stopped discarding anything. With the spec, a netmiko rename turns
into an AttributeError here.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from netmiko.nokia.nokia_srl import NokiaSrlSSH

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


def _conn(set_output="(ok)", commit_output="All changes have been committed."):
    """A netmiko connection double specced against the real nokia_srl class.

    spec=NokiaSrlSSH is load-bearing, not tidiness: the driver's candidate
    cleanup goes through conn._discard() and conn.config_mode(), both
    netmiko-internal. Without the spec a rename upstream would leave these
    tests green while the driver discarded nothing at all.
    """
    conn = MagicMock(spec=NokiaSrlSSH)
    conn.send_config_set.return_value = set_output
    conn.commit.return_value = commit_output
    return conn


def test_netmiko_still_exposes_the_private_candidate_helpers():
    """The driver depends on two netmiko-internal methods. Fail HERE, loudly,
    if a netmiko upgrade renames either, rather than silently losing the
    discard (issue #778)."""
    assert hasattr(NokiaSrlSSH, "_discard")
    assert hasattr(NokiaSrlSSH, "config_mode")


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
    conn = _conn()
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).create_vlan(100)
    conn.send_config_set.assert_called_once_with(["set / network-instance vlan100 type mac-vrf"])
    conn.commit.assert_called_once()
    assert result == {"success": True}


def test_add_to_vlan_tagged_sends_exact_commands():
    conn = _conn()
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
    conn = _conn()
    with patch("netmiko.ConnectHandler", return_value=conn):
        Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/1", vlan_id=100)
    sent = conn.send_config_set.call_args[0][0]
    assert (
        "set / interface ethernet-1/1 subinterface 100 vlan encap single-tagged vlan-id 100" in sent
    )


def test_add_to_vlan_untagged_sends_exact_commands():
    conn = _conn()
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
    conn = _conn()
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
    conn = _conn(commit_output="Nothing to commit.")
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).delete_vlan(100)
    conn.send_config_set.assert_called_once_with(["delete / network-instance vlan100"])
    conn.commit.assert_called_once()
    assert result == {"success": True}


# --- device rejection surfaces as success: False, never an exception -----------
#
# HERD keys provisioning success on the driver's returned payload (execution's
# driver_result_failed helper), so a false "success" for a command the switch
# actually rejected would make HERD record an ACTIVE VLAN membership that was
# never really applied. SR Linux's "Nothing to commit." is ambiguous on its
# own: a rejected `set` never stages, so commit says the same thing a genuine
# idempotent no-op says. These tests pin the error-marker scan that tells the
# two apart, on both the set output and the commit output, plus the candidate
# discipline of issue #778: the entry discard, the discard on each failure
# path, and the rule that a set-time rejection must never reach commit().


def test_create_vlan_returns_failure_when_set_output_has_parsing_error():
    conn = _conn(
        set_output="Parsing error: Unknown token 'network-instance'.",
        commit_output="commit stay\nNothing to commit. Starting new transaction.",
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).create_vlan(100)
    assert result["success"] is False
    assert "Parsing error:" in result["error"]
    # Twice: the entry discard, then the failure discard. Both matter.
    assert conn._discard.call_count == 2, conn._discard.call_args_list


def test_add_to_vlan_returns_failure_when_commit_output_has_invalid_value():
    conn = _conn(
        set_output="(ok, no error in the set output)",
        commit_output=(
            'Invalid value "9999": Does not match any of the union types:\n'
            "    Must be an integer in range 1..4094"
        ),
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/1", vlan_id=9999, tag="tagged")
    assert result["success"] is False
    assert "Invalid value" in result["error"]
    # Twice: the entry discard, then the failure discard. Both matter.
    assert conn._discard.call_count == 2, conn._discard.call_args_list


def test_remove_from_vlan_returns_failure_on_error_prefixed_commit_line():
    """A config that parses cleanly can still be refused only at commit
    time, e.g. a semantic inconsistency between two otherwise-valid lines
    in the same batch (reproduced live: "vlan tagging true inconsistent
    with subinterface 9999"). That surfaces as a line starting with
    "Error:" rather than "Parsing error:" or "Invalid value"."""
    conn = _conn(
        set_output="(ok, no error in the set output)",
        commit_output=(
            "commit stay\n"
            "Error in /interface[name=ethernet-1/1]/vlan-tagging:\n"
            "    vlan tagging true inconsistent with subinterface 9999\n"
            "Error: Commit failed"
        ),
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).remove_from_vlan(port="ethernet-1/1", vlan_id=9999)
    assert result["success"] is False
    assert "Commit failed" in result["error"]
    # Twice: the entry discard, then the failure discard. Both matter.
    assert conn._discard.call_count == 2, conn._discard.call_args_list


def test_clean_apply_returns_success_and_discards_only_at_entry():
    conn = _conn(
        set_output="set / network-instance vlan100 type mac-vrf",
        commit_output="commit stay\nAll changes have been committed.",
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).create_vlan(100)
    assert result == {"success": True}
    # The entry discard runs unconditionally; a clean commit adds no second one.
    conn._discard.assert_called_once()


def test_nothing_to_commit_with_no_error_marker_is_still_success():
    """The idempotent-no-op case, pinned distinctly from the failure case
    above: both produce "Nothing to commit.", but only the failure case
    carries an error marker anywhere in the set or commit output."""
    conn = _conn(
        set_output="set / network-instance vlan100 type mac-vrf",
        commit_output="commit stay\nNothing to commit. Starting new transaction.",
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).create_vlan(100)
    assert result == {"success": True}
    # The entry discard runs unconditionally; a clean commit adds no second one.
    conn._discard.assert_called_once()


# --- commit is issued for every mutating operation, one session is reused ------


def test_commit_is_issued_for_every_mutating_operation_over_one_shared_session():
    conn = _conn()
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
    conn = _conn()
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


# --- issue #778: candidate discipline ----------------------------------------
#
# SR Linux stages `set`/`delete` in a per-user private candidate that only
# `commit stay` applies, and that candidate outlives the SSH session it was
# staged in (proven live; see the driver's CANDIDATE DISCIPLINE docstring).
# netmiko also keeps sending after a rejected line, so a commit issued before
# the set output is judged applies the valid PREFIX of a batch the caller is
# being told failed. These tests pin the three rules that follow from that.


def _call_names(conn):
    """Ordered netmiko method names the driver invoked on the connection."""
    return [name for name, _args, _kwargs in conn.mock_calls]


def test_set_time_rejection_never_reaches_commit():
    """The headline of issue #778: a rejected batch must not be committed.

    Live evidence: a two-line batch whose second line was a parsing error
    still staged the first line, and the commit that followed answered "All
    changes have been committed.", leaving the device half-configured while
    the driver returned success: False.
    """
    conn = _conn(
        set_output=(
            "set / interface ethernet-1/3 admin-state enable\n"
            "set / interface ethernet-1/3 vlan-taggingX true\n"
            "Parsing error: Unknown token 'vlan-taggingX'."
        )
    )
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/3", vlan_id=100)
    assert result["success"] is False
    conn.commit.assert_not_called()
    assert conn._discard.call_count == 2, conn._discard.call_args_list


def test_discard_runs_before_anything_is_staged():
    """The entry discard, which is what protects this call from a candidate
    a previous, dead call left behind on the device."""
    conn = _conn()
    with patch("netmiko.ConnectHandler", return_value=conn):
        assert Driver(_REAL_CTX).create_vlan(100) == {"success": True}
    names = _call_names(conn)
    assert "_discard" in names, names
    assert names.index("_discard") < names.index("send_config_set"), names


def test_entry_discard_enters_candidate_mode_first():
    """`discard stay` in running mode is itself a "Parsing error:" (verified
    live), and a fresh session starts in running mode, so the discard has to
    enter candidate mode before it can clear anything."""
    conn = _conn()
    with patch("netmiko.ConnectHandler", return_value=conn):
        Driver(_REAL_CTX).create_vlan(100)
    names = _call_names(conn)
    assert names.index("config_mode") < names.index("_discard"), names


def test_exception_while_staging_discards_before_propagating():
    """The sandbox runs one process per action, but the candidate lives on the
    DEVICE, so a call that dies mid-batch must not leave lines staged for the
    next call's commit to pick up."""
    conn = _conn()
    conn.send_config_set.side_effect = OSError("connection reset")
    with patch("netmiko.ConnectHandler", return_value=conn):
        with pytest.raises(OSError):
            Driver(_REAL_CTX).create_vlan(100)
    assert conn._discard.call_count == 2, conn._discard.call_args_list


def test_exception_during_commit_discards_before_propagating():
    conn = _conn()
    conn.commit.side_effect = OSError("connection reset")
    with patch("netmiko.ConnectHandler", return_value=conn):
        with pytest.raises(OSError):
            Driver(_REAL_CTX).create_vlan(100)
    assert conn._discard.call_count == 2, conn._discard.call_args_list


def test_a_failing_discard_never_masks_the_real_outcome():
    """Cleanup is best-effort by design: the next call's entry discard absorbs
    whatever this one could not clear, so a discard that itself fails must not
    turn a reported rejection into a raised exception."""
    conn = _conn(set_output="Parsing error: Unknown token 'network-instance'.")
    conn._discard.side_effect = OSError("connection reset")
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).create_vlan(100)
    assert result["success"] is False
    assert "Parsing error:" in result["error"]


def test_error_is_the_narrowed_offending_line_not_the_whole_transcript():
    """`error` lands in a wiring assignment's last_error column, so it stays
    one readable line; the full device output rides alongside under `output`
    (commit 722a2842)."""
    transcript = (
        "set / interface ethernet-1/3 admin-state enable\n"
        "--{ +* candidate private private-admin }--[  ]--\n"
        "set / interface ethernet-1/3 vlan-taggingX true\n"
        "Parsing error: Unknown token 'vlan-taggingX'."
    )
    conn = _conn(set_output=transcript)
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = Driver(_REAL_CTX).add_to_vlan(port="ethernet-1/3", vlan_id=100)
    assert result["error"] == "Parsing error: Unknown token 'vlan-taggingX'."
    assert "admin-state enable" not in result["error"]
    assert "admin-state enable" in result["output"]


# --- issue #780: optional HERD_port parsing ----------------------------------
#
# Device field_data reaches the driver context verbatim, so an optional port
# field the operator left alone arrives as "" and int("") raises. Parsing it in
# __init__ therefore broke the "status() never raises" contract before any
# method could run. Blank or missing means 22; a non-integer raises from
# _connect, where status() can still degrade to {"reachable": False}.


def test_blank_herd_port_defaults_to_22():
    conn = _conn()
    conn.send_command.return_value = "OS : SR Linux"
    ctx = {**_REAL_CTX, "HERD_port": ""}
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        assert Driver(ctx).status() == {"reachable": True}
    assert ch.call_args.kwargs["port"] == 22


def test_missing_herd_port_defaults_to_22():
    conn = _conn()
    conn.send_command.return_value = "OS : SR Linux"
    ctx = {k: v for k, v in _REAL_CTX.items() if k != "HERD_port"}
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        assert Driver(ctx).status() == {"reachable": True}
    assert ch.call_args.kwargs["port"] == 22


def test_numeric_string_herd_port_is_parsed():
    conn = _conn()
    conn.send_command.return_value = "OS : SR Linux"
    ctx = {**_REAL_CTX, "HERD_port": "2223"}
    with patch("netmiko.ConnectHandler", return_value=conn) as ch:
        Driver(ctx).status()
    assert ch.call_args.kwargs["port"] == 2223


def test_non_integer_herd_port_does_not_raise_from_the_constructor():
    ctx = {**_REAL_CTX, "HERD_port": "abc"}
    driver = Driver(ctx)  # must not raise: health polling constructs this
    assert driver is not None


def test_non_integer_herd_port_degrades_status_instead_of_raising():
    ctx = {**_REAL_CTX, "HERD_port": "abc"}
    with patch("netmiko.ConnectHandler") as ch:
        result = Driver(ctx).status()
    assert result["reachable"] is False
    assert "HERD_port must be an integer" in result["error"]
    ch.assert_not_called()


def test_non_integer_herd_port_raises_driver_error_from_a_mutating_call():
    ctx = {**_REAL_CTX, "HERD_port": "abc"}
    with patch("netmiko.ConnectHandler") as ch:
        with pytest.raises(DriverError, match="HERD_port must be an integer"):
            Driver(ctx).create_vlan(100)
    ch.assert_not_called()
