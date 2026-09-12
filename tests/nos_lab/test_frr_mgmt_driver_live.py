"""Live tests for drivers/frr_mgmt (the Management reference driver) against
the checked-in emulated-gear test lab's FRR node (infra/nos-test,
docs/NOS_LAB.md).

Mirrors tests/nos_lab/test_frr_l3_driver_live.py's gating and helper style
exactly: skipped automatically when the FRR node is not reachable, so the
suite stays green on CI and on any host that has not run `make nos-up`.
Setting HERD_TEST_NOS_REQUIRED=1 disables the skip, turning an unreachable
lab into a hard failure with a `make nos-up` remedy instead of a silent
no-op.

These tests instantiate the real drivers/frr_mgmt Driver class the way the
execution service would, pointed at 127.0.0.1:2224 with the netadmin/netadmin
credentials, and verify every change independently of the driver's own
session (a separate `docker exec nos-test-frr vtysh -c ...` call, never the
driver's own backup()/status()). Asserting through the driver's own read path
would let a driver that lies consistently pass its own test; this is the
exact anti-pattern issue #771 and docs/DRIVERS.md's "A driver must report a
device rejection as a failure" section call out.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import random
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_PATH = _REPO_ROOT / "drivers" / "frr_mgmt" / "driver.py"

_spec = importlib.util.spec_from_file_location("frr_mgmt_driver_live", _DRIVER_PATH)
frr_mgmt_driver = importlib.util.module_from_spec(_spec)
sys.modules["frr_mgmt_driver_live"] = frr_mgmt_driver
_spec.loader.exec_module(frr_mgmt_driver)

Driver = frr_mgmt_driver.Driver

FRR_HOST = os.getenv("HERD_TEST_FRR_HOST", "127.0.0.1")
FRR_PORT = int(os.getenv("HERD_TEST_FRR_PORT", "2224"))
FRR_USERNAME = "netadmin"
FRR_PASSWORD = "netadmin"
FRR_CONTAINER = "nos-test-frr"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_FRR_REACHABLE = _reachable(FRR_HOST, FRR_PORT)
_NOS_REQUIRED = os.getenv("HERD_TEST_NOS_REQUIRED", "") not in ("", "0")

pytestmark = pytest.mark.skipif(
    not _NOS_REQUIRED and not _FRR_REACHABLE,
    reason=(
        f"NOS test lab FRR node not reachable ({FRR_HOST}:{FRR_PORT}); "
        "start it with `make nos-up` to run."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _NOS_REQUIRED and not _FRR_REACHABLE:
        pytest.fail(
            f"HERD_TEST_NOS_REQUIRED is set but the NOS test lab FRR node is not "
            f"reachable ({FRR_HOST}:{FRR_PORT}); run `make nos-up` (infra/nos-test) "
            "or unset HERD_TEST_NOS_REQUIRED."
        )


def _docker_exec(container: str, *args: str) -> str:
    """Run a command inside the lab container and return its stdout.

    The independent verification channel: never goes through the driver's
    own netmiko session, so it proves the device's own state, not just that
    the driver believes it succeeded.
    """
    result = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"docker exec {container} {' '.join(args)} failed (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout


def _show_ip_route_static() -> str:
    return _docker_exec(FRR_CONTAINER, "vtysh", "-c", "show ip route static")


def _show_running_config() -> str:
    return _docker_exec(FRR_CONTAINER, "vtysh", "-c", "show running-config")


def _unique_test_prefix() -> str:
    """Return a random /30 destination prefix inside RFC5737 TEST-NET-1
    (192.0.2.0/24) so concurrent or repeated runs never collide on the
    destination. 192.0.2.0/24 has 64 non-overlapping /30s."""
    network = ipaddress.ip_network("192.0.2.0/24")
    subnets = list(network.subnets(new_prefix=30))
    subnet = random.choice(subnets)
    return f"{subnet.network_address}/{subnet.prefixlen}"


def _frr_connected_nexthop() -> str:
    """Return an address on the FRR container's own connected subnet, so a
    static route toward it resolves and is SELECTED (and therefore visible
    under `show ip route static`) rather than sitting unresolved. Mirrors
    test_frr_l3_driver_live.py's helper of the same name exactly."""
    addr_output = _docker_exec(FRR_CONTAINER, "ip", "-4", "-o", "addr", "show", "eth0")
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr_output)
    assert match, f"could not parse eth0 address from: {addr_output!r}"
    iface = ipaddress.ip_interface(match.group(1))
    candidate = iface.network.broadcast_address - 1
    if candidate == iface.ip:
        candidate = iface.network.broadcast_address - 2
    return str(candidate)


def _context(**extra) -> dict:
    return {
        "HERD_ip": FRR_HOST,
        "HERD_login": FRR_USERNAME,
        "HERD_password": FRR_PASSWORD,
        "HERD_port": FRR_PORT,
        **extra,
    }


def _cleanup_route(destination: str, next_hop: str) -> None:
    """Best-effort removal via a direct vtysh call (not the driver under
    test), so a failing assertion earlier in a test never leaves the lab
    node's running OR startup config dirty. Runs regardless of whether the
    route was actually installed."""
    subprocess.run(
        [
            "docker",
            "exec",
            FRR_CONTAINER,
            "vtysh",
            "-c",
            "configure terminal",
            "-c",
            f"no ip route {destination} {next_hop}",
            "-c",
            "end",
            "-c",
            "write memory",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# configure(): a clean apply is independently verifiable and persisted.
# ---------------------------------------------------------------------------


def test_configure_clean_apply_is_independently_verifiable():
    destination = _unique_test_prefix()
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        assert d.login()["success"] is True
        result = d.configure(commands=[f"ip route {destination} {next_hop}"])
        assert result["success"] is True
        assert "error" not in result

        # Independent verification: a fresh vtysh call, never the driver's session.
        routes = _show_ip_route_static()
        assert destination in routes, routes

        # configure() persists to startup config (write memory); confirm that too.
        running = _show_running_config()
        assert destination in running, running

        remove_result = d.configure(commands=[f"no ip route {destination} {next_hop}"])
        assert remove_result["success"] is True

        routes_after = _show_ip_route_static()
        assert destination not in routes_after, routes_after
    finally:
        d.logout()
        _cleanup_route(destination, next_hop)


# ---------------------------------------------------------------------------
# Rejected config must report failure, not success (regression test for
# issue #771: the driver never inspected vtysh output, so it reported
# {"success": True} for a command the device rejected outright, while
# `show ip route static` independently proved nothing was installed). This
# asserts BOTH halves: the driver's own return value, and an independent
# verification that the device never accepted the command.
# ---------------------------------------------------------------------------


def test_configure_rejected_by_device_reports_failure_and_installs_nothing():
    # A malformed destination the device rejects outright ("% Unknown command").
    malformed_destination = "999.999.999.0/24"
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        d.login()
        result = d.configure(commands=[f"ip route {malformed_destination} {next_hop}"])

        # Half 1: the driver's own return value must say failure, with the
        # offending line surfaced (not a whole transcript).
        assert result["success"] is False, (
            f"driver reported success for a command the device rejected: {result!r}"
        )
        assert "error" in result
        assert result["error"].startswith("%")
        assert malformed_destination in result["error"]

        # Half 2: independent verification, never through the driver's own
        # read path, that nothing was actually installed or persisted.
        routes = _show_ip_route_static()
        assert malformed_destination not in routes, routes
        running = _show_running_config()
        assert malformed_destination not in running, running
    finally:
        d.logout()
        # Nothing should have been installed, but clean up defensively in
        # case a future regression reintroduces the bug this test guards.
        _cleanup_route(malformed_destination, next_hop)


# ---------------------------------------------------------------------------
# The Management driver's benign classification (Lane's ruling): removing an
# already-absent route is judged by desired end state (the route is gone,
# which is what the caller wanted), not by whether the device complained, so
# it must report success, the same as drivers/frr_l3's remove_route does for
# the identical device text.
# ---------------------------------------------------------------------------


def test_configure_removing_already_absent_route_reports_success():
    destination = _unique_test_prefix()
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        d.login()
        # Confirm the route is not present, then try to remove it anyway.
        assert destination not in _show_ip_route_static()
        result = d.configure(commands=[f"no ip route {destination} {next_hop}"])
        assert result["success"] is True, (
            "removing an already-absent route should converge to the desired "
            f"end state (the route is gone) and report success: {result!r}"
        )
        assert "error" not in result
        assert result["benign_warnings"] == ["% Refusing to remove a non-existent route"]

        # The end state itself: still gone, nothing was disturbed.
        assert destination not in _show_ip_route_static()
    finally:
        d.logout()
        _cleanup_route(destination, next_hop)


# ---------------------------------------------------------------------------
# The echoed-marker rejection (issue #779, item 1): FRR quotes the offending
# command back inside "% Unknown command: <the command>", so a config line
# that merely CONTAINS the benign "already absent" phrase used to be carved
# out as benign by an unanchored substring test and reported as success. The
# published config schema accepts any string up to 512 characters, so nothing
# upstream filters such a line.
# ---------------------------------------------------------------------------


def test_configure_echoed_benign_marker_is_reported_as_a_genuine_failure():
    command = "ip route Refusing to remove a non-existent route"

    d = Driver(_context())
    try:
        d.login()
        result = d.configure(commands=[command])
        assert result["success"] is False, (
            f"an echoed benign phrase inside a genuine rejection must not be carved out: {result!r}"
        )
        assert result["error"].startswith("% Unknown command")
        assert "benign_warnings" not in result

        # Independent verification: nothing was installed or persisted.
        assert "Refusing" not in _show_ip_route_static()
        assert "Refusing" not in _show_running_config()
    finally:
        d.logout()


# ---------------------------------------------------------------------------
# The masking bug this ruling closes: a benign line arriving BEFORE a genuine
# rejection in the same multi-command batch must not hide the genuine one.
# Driven live in one configure() call: the first line is a no-op removal of
# an already-absent route (benign), the second is a malformed destination
# the device rejects outright (genuine). The overall result must be failure,
# reporting the genuine line, and nothing from the batch may be installed.
# ---------------------------------------------------------------------------


def test_configure_genuine_failure_after_a_benign_line_is_not_masked():
    absent_destination = _unique_test_prefix()
    malformed_destination = "999.999.999.0/24"
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        d.login()
        assert absent_destination not in _show_ip_route_static()

        result = d.configure(
            commands=[
                f"no ip route {absent_destination} {next_hop}",  # benign, comes first
                f"ip route {malformed_destination} {next_hop}",  # genuine, comes second
            ]
        )
        assert result["success"] is False, (
            f"a benign line before a genuine rejection must not mask it: {result!r}"
        )
        assert malformed_destination in result["error"]
        assert "Refusing to remove a non-existent route" not in result["error"]

        # Independent verification: the rejected line installed nothing.
        routes = _show_ip_route_static()
        assert malformed_destination not in routes, routes
        running = _show_running_config()
        assert malformed_destination not in running, running
    finally:
        d.logout()
        _cleanup_route(malformed_destination, next_hop)


# ---------------------------------------------------------------------------
# A config that applied but could not be PERSISTED is a failure (issue #779,
# item 2). vtysh reports a failed `write memory` with no "%" line at all and
# exits 0, so the rejection scan cannot see it; configure() has to require
# positive evidence of a save instead.
#
# Making the save fail on demand takes two steps, both verified live
# 2026-09-12 on this node:
#
#   1. Move /etc/frr/frr.conf aside, if present. With an integrated config
#      file, vtysh hands `write memory` to watchfrr, which runs as ROOT and
#      saves successfully whatever the directory mode says. Only the
#      per-daemon path, which vtysh writes itself as the logged-in user, can
#      be made to fail.
#   2. chmod 555 /etc/frr, NOT 000. Under 000 vtysh cannot even stat
#      frr.conf, falls back to treating the config as integrated, and the
#      root-owned watchfrr write succeeds again (and recreates frr.conf).
#      555 leaves the directory readable and traversable, so vtysh takes the
#      per-daemon path and then cannot create its temp files.
#
# Both the mode and the stashed file are restored in `finally`, and the
# restored mode is asserted, so a failure here never leaves the lab node
# unwritable for the next test.
# ---------------------------------------------------------------------------

_INTEGRATED_CONFIG = "/etc/frr/frr.conf"
_INTEGRATED_CONFIG_STASH = "/tmp/frr.conf.herd-live-test"


def _etc_frr_mode() -> str:
    return _docker_exec(FRR_CONTAINER, "stat", "-c", "%a", "/etc/frr").strip()


def _try_docker_exec(*args: str) -> int:
    """Run a command in the lab container, returning its exit status.

    Unlike _docker_exec, a non-zero exit is an answer, not an assertion
    failure: the stash step below is a "move it if it is there" probe.
    """
    return subprocess.run(
        ["docker", "exec", FRR_CONTAINER, *args],
        capture_output=True,
        text=True,
        timeout=15,
    ).returncode


def test_configure_reports_failure_when_the_startup_config_cannot_be_saved():
    destination = _unique_test_prefix()
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    stashed = False
    try:
        stashed = (
            _try_docker_exec("sh", "-c", f"mv -f {_INTEGRATED_CONFIG} {_INTEGRATED_CONFIG_STASH}")
            == 0
        )
        _docker_exec(FRR_CONTAINER, "chmod", "555", "/etc/frr")
        assert _etc_frr_mode() == "555"

        d.login()
        result = d.configure(commands=[f"ip route {destination} {next_hop}"])

        # Half 1: the driver's own return value. An unpersisted config is not
        # a success, and the operator-facing `error` is the line the device
        # actually printed.
        assert result["success"] is False, (
            f"driver reported success for a config it could not persist: {result!r}"
        )
        assert "Can't open configuration file" in result["error"], result
        assert "save_output" in result
        assert result["attempted"] == [f"ip route {destination} {next_hop}"]
        assert "applied" not in result

        # Half 2: independent verification (a separate vtysh call, never the
        # driver's session) that the failure really is about persistence: the
        # line DID reach the running config, which is exactly why reporting
        # success here would leave HERD's ledger agreeing with a device that
        # loses the route on its next daemon restart.
        assert destination in _show_ip_route_static()
    finally:
        d.logout()
        _docker_exec(FRR_CONTAINER, "chmod", "755", "/etc/frr")
        _docker_exec(FRR_CONTAINER, "chown", "frr:frr", "/etc/frr")
        if stashed:
            # Remove anything recreated while the directory was locked down,
            # then put the node's own integrated config back.
            _try_docker_exec("sh", "-c", f"rm -f {_INTEGRATED_CONFIG}")
            _try_docker_exec("sh", "-c", f"mv -f {_INTEGRATED_CONFIG_STASH} {_INTEGRATED_CONFIG}")
        _cleanup_route(destination, next_hop)
        assert _etc_frr_mode() == "755", "the lab node's /etc/frr mode was not restored"


# ---------------------------------------------------------------------------
# backup(): a clean read against the live node succeeds and is not confused
# with a rejection.
# ---------------------------------------------------------------------------


def test_backup_reads_running_config_from_live_node():
    d = Driver(_context())
    try:
        d.login()
        result = d.backup()
        assert result["success"] is True
        assert "error" not in result
        assert "frr version" in result["config"]
    finally:
        d.logout()


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_reports_reachable_against_the_live_node():
    d = Driver(_context())
    try:
        d.login()
        result = d.status()
        assert result["reachable"] is True
    finally:
        d.logout()
