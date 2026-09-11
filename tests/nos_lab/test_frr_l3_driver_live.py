"""Live tests for drivers/frr_l3 (the Layer 3 Switch reference driver) against
the checked-in emulated-gear test lab's FRR node (infra/nos-test, docs/NOS_LAB.md).

Mirrors tests/nos_lab/test_nos_lab_live.py's gating and helper style exactly:
skipped automatically when the FRR node is not reachable, so the suite stays
green on CI and on any host that has not run `make nos-up`. Setting
HERD_TEST_NOS_REQUIRED=1 disables the skip, turning an unreachable lab into a
hard failure with a `make nos-up` remedy instead of a silent no-op.

Unlike test_nos_lab_live.py (which drives the lab directly via netmiko), these
tests instantiate the real drivers/frr_l3 Driver class the way the execution
service would, pointed at 127.0.0.1:2224 with the netadmin/netadmin
credentials, and verify every change independently of the driver's own session
(a separate `docker exec nos-test-frr vtysh -c ...` call, never the driver's
own status()). Asserting through the driver's own read path would let a driver
that lies consistently pass its own test.
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
_DRIVER_PATH = _REPO_ROOT / "drivers" / "frr_l3" / "driver.py"

_spec = importlib.util.spec_from_file_location("frr_l3_driver_live", _DRIVER_PATH)
frr_l3_driver = importlib.util.module_from_spec(_spec)
sys.modules["frr_l3_driver_live"] = frr_l3_driver
_spec.loader.exec_module(frr_l3_driver)

Driver = frr_l3_driver.Driver

FRR_HOST = os.getenv("HERD_TEST_FRR_HOST", "127.0.0.1")
FRR_PORT = int(os.getenv("HERD_TEST_FRR_PORT", "2224"))
FRR_USERNAME = "netadmin"
FRR_PASSWORD = "netadmin"
FRR_CONTAINER = "nos-test-frr"
FRR_INTERFACE = "eth0"


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

    The independent verification channel: never goes through the driver's own
    netmiko session, so it proves the device's own state, not just that the
    driver believes it succeeded.
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
    under `show ip route static`) rather than sitting unresolved. A route
    toward an unreachable RFC5737 next hop never becomes RIB-selected and is
    silently absent from `show ip route static` (verified live), so this
    helper follows test_nos_lab_live.py's approach exactly rather than
    hardcoding an address."""
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


def _cleanup_route(destination: str, next_hop: str | None, interface: str) -> None:
    """Best-effort removal via the driver, plus a direct vtysh fallback, so a
    failing assertion earlier in a test never leaves the lab node dirty."""
    try:
        d = Driver(_context())
        d.login()
        d.remove_route(destination=destination, next_hop=next_hop, interface=interface)
        d.logout()
    finally:
        # Fallback: remove directly, ignoring errors (it may already be gone).
        cmd = (
            f"no ip route {destination} {next_hop}"
            if next_hop is not None
            else f"no ip route {destination} {interface}"
        )
        subprocess.run(
            ["docker", "exec", FRR_CONTAINER, "vtysh", "-c", "configure terminal", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )


# ---------------------------------------------------------------------------
# configure_route / remove_route (next-hop form), verified independently.
# ---------------------------------------------------------------------------


def test_configure_and_remove_route_next_hop_form_is_independently_verifiable():
    destination = _unique_test_prefix()
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        assert d.login()["success"] is True
        result = d.configure_route(
            destination=destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        assert result["success"] is True

        # Independent verification: a fresh vtysh call, never the driver's session.
        routes = _show_ip_route_static()
        assert destination in routes, routes

        remove_result = d.remove_route(
            destination=destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        assert remove_result["success"] is True

        routes_after = _show_ip_route_static()
        assert destination not in routes_after, routes_after
    finally:
        d.logout()
        _cleanup_route(destination, next_hop, FRR_INTERFACE)


# ---------------------------------------------------------------------------
# configure_route / remove_route (interface-route form, next_hop=None).
# ---------------------------------------------------------------------------


def test_configure_and_remove_route_interface_form_is_independently_verifiable():
    destination = _unique_test_prefix()

    d = Driver(_context())
    try:
        assert d.login()["success"] is True
        result = d.configure_route(destination=destination, next_hop=None, interface=FRR_INTERFACE)
        assert result["success"] is True

        routes = _show_ip_route_static()
        assert destination in routes, routes

        remove_result = d.remove_route(
            destination=destination, next_hop=None, interface=FRR_INTERFACE
        )
        assert remove_result["success"] is True

        routes_after = _show_ip_route_static()
        assert destination not in routes_after, routes_after
    finally:
        d.logout()
        _cleanup_route(destination, None, FRR_INTERFACE)


# ---------------------------------------------------------------------------
# Idempotency, live evidence for the report's decision:
#   - re-configuring an already-configured route succeeds and changes nothing
#     observable (FRR treats the duplicate `ip route` line as a no-op).
#   - removing an already-removed route still reports success (the desired
#     end state, route absent, already holds), even though the underlying
#     vtysh CLI answers "% Refusing to remove a non-existent route".
# ---------------------------------------------------------------------------


def test_configure_route_is_idempotent_when_reapplied():
    destination = _unique_test_prefix()
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        d.login()
        first = d.configure_route(
            destination=destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        assert first["success"] is True

        routes_after_first = _show_ip_route_static()
        assert destination in routes_after_first

        # Re-apply the identical route: must succeed and leave exactly one
        # entry for this destination (no duplicate, no error).
        second = d.configure_route(
            destination=destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        assert second["success"] is True

        routes_after_second = _show_ip_route_static()
        assert routes_after_second.count(destination) == routes_after_first.count(destination)
    finally:
        d.logout()
        _cleanup_route(destination, next_hop, FRR_INTERFACE)


def test_remove_route_is_idempotent_when_route_already_gone():
    destination = _unique_test_prefix()
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        d.login()
        d.configure_route(destination=destination, next_hop=next_hop, interface=FRR_INTERFACE)
        assert destination in _show_ip_route_static()

        first_remove = d.remove_route(
            destination=destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        assert first_remove["success"] is True
        assert destination not in _show_ip_route_static()

        # Second removal of the now-absent route: the live driver contract
        # decision (see drivers/frr_l3/driver.py's module docstring and the
        # PR report) is that this still reports success, since the desired
        # end state already holds. Verify the node stays clean either way.
        second_remove = d.remove_route(
            destination=destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        assert second_remove["success"] is True
        assert destination not in _show_ip_route_static()
    finally:
        d.logout()
        _cleanup_route(destination, next_hop, FRR_INTERFACE)


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
