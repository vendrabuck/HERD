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

# The VRF fixture infra/nos-test/frr/start.sh creates at boot (ADR 0014 addendum
# X-G, issue #755; docs/NOS_LAB.md). `blue` maps to routing table 10 and owns
# `dummy0` at 192.0.2.254/30, so a next hop inside 192.0.2.252/30 resolves
# through it. The fixture is permanent: FRR refuses `no vrf <name>` while the
# Linux device exists, so these tests clean up only their own routes.
VRF_NAME = "blue"
VRF_TABLE = "10"
VRF_INTERFACE = "dummy0"
VRF_MEMBER_SUBNET = ipaddress.ip_network("192.0.2.252/30")
VRF_NEXT_HOP = "192.0.2.253"
UNKNOWN_VRF_NAME = "no-such-vrf"


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


def _vrf_fixture_reason() -> str:
    """Empty when the lab node carries the VRF fixture; otherwise the reason,
    naming the remedy.

    The fixture depends on the DOCKER HOST's kernel: `ip link add ... type vrf`
    needs the host's `vrf` module (and `dummy` for the member interface), which a
    container cannot load for itself, so `infra/nos-test/frr/start.sh` creates it
    best-effort and boots the node either way (a GitHub Actions runner without
    the modules is the known case). Probing here, rather than asserting inside
    each test, keeps a host-capability gap a visible SKIP with a fix attached
    instead of a red test that says nothing about the cause.
    """
    if not _FRR_REACHABLE:
        return ""  # the lab-reachability gate above already covers this
    try:
        links = subprocess.run(
            ["docker", "exec", FRR_CONTAINER, "ip", "-br", "link"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not probe the FRR node for the VRF fixture: {exc}"
    if links.returncode != 0:
        return f"could not probe the FRR node for the VRF fixture: {links.stderr.strip()}"
    if VRF_NAME not in links.stdout or VRF_INTERFACE not in links.stdout:
        return (
            f"the lab's FRR node carries no `{VRF_NAME}` VRF fixture (the Docker host "
            "is probably missing the vrf/dummy kernel modules): run "
            "`sudo modprobe vrf dummy` on the host, then `make nos-reset`. "
            "See docs/NOS_LAB.md."
        )
    return ""


_VRF_FIXTURE_REASON = _vrf_fixture_reason()

# Applied to every test that needs the VRF fixture. Note this is a SKIP even
# under HERD_TEST_NOS_REQUIRED=1, which is about the lab being REACHABLE, not
# about the host kernel's feature set.
needs_vrf_fixture = pytest.mark.skipif(bool(_VRF_FIXTURE_REASON), reason=_VRF_FIXTURE_REASON)


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
    destination. 192.0.2.0/24 has 64 non-overlapping /30s, minus the one the
    lab's VRF member interface occupies (VRF_MEMBER_SUBNET): a destination
    equal to a connected subnet is not a static route the RIB would select."""
    network = ipaddress.ip_network("192.0.2.0/24")
    subnets = [s for s in network.subnets(new_prefix=30) if s != VRF_MEMBER_SUBNET]
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
# Rejected routes must report failure, not success (regression test for the
# blocking bug: the driver initially reported {"success": True} for a route
# vtysh answered "% Unknown command" for, while `show ip route static`
# independently proved nothing was installed). This asserts BOTH halves: the
# driver's own return value, and an independent verification that the device
# never accepted the route.
# ---------------------------------------------------------------------------


def test_configure_route_rejected_by_device_reports_failure_and_installs_nothing():
    # A malformed destination the device rejects outright ("% Unknown command").
    malformed_destination = "999.999.999.0/24"
    next_hop = _frr_connected_nexthop()

    d = Driver(_context())
    try:
        d.login()
        result = d.configure_route(
            destination=malformed_destination, next_hop=next_hop, interface=FRR_INTERFACE
        )
        # Half 1: the driver's own return value must say failure.
        assert result["success"] is False, (
            f"driver reported success for a route the device rejected: {result!r}"
        )
        assert "error" in result

        # Half 2: independent verification, never through the driver's own
        # read path, that nothing was actually installed.
        routes = _show_ip_route_static()
        assert malformed_destination not in routes, routes
    finally:
        d.logout()
        # Nothing should have been installed, but clean up defensively in case
        # a future regression reintroduces the bug this test guards against.
        _cleanup_route(malformed_destination, next_hop, FRR_INTERFACE)


# ---------------------------------------------------------------------------
# VRF routes (ADR 0014 addenda X-G and X-H, issue #755), against the lab's
# checked-in VRF fixture. Verification is doubly independent: not only a
# separate `docker exec` rather than the driver's own session, but TWO
# channels, the kernel routing table the VRF maps to (`ip route show table 10`,
# which does not go through FRR at all) and FRR's own per-VRF view.
# ---------------------------------------------------------------------------


def _vrf_kernel_routes() -> str:
    return _docker_exec(FRR_CONTAINER, "ip", "route", "show", "table", VRF_TABLE)


def _vrf_frr_routes() -> str:
    return _docker_exec(FRR_CONTAINER, "vtysh", "-c", f"show ip route vrf {VRF_NAME} static")


def _cleanup_vrf_route(destination: str, next_hop: str | None) -> None:
    cmd = (
        f"no ip route {destination} {next_hop} vrf {VRF_NAME}"
        if next_hop is not None
        else f"no ip route {destination} {VRF_INTERFACE} vrf {VRF_NAME}"
    )
    subprocess.run(
        ["docker", "exec", FRR_CONTAINER, "vtysh", "-c", "configure terminal", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=15,
    )


@needs_vrf_fixture
def test_the_lab_node_carries_the_vrf_fixture():
    """The precondition the two tests below rest on: start.sh created VRF `blue`
    (table 10) with `dummy0` as a member. A lab built from an older image would
    otherwise make the VRF tests fail for a reason that has nothing to do with
    the driver; `make nos-reset` is the remedy."""
    links = _docker_exec(FRR_CONTAINER, "ip", "-br", "link")
    assert VRF_NAME in links, links
    assert VRF_INTERFACE in links, links
    addrs = _docker_exec(FRR_CONTAINER, "ip", "-4", "-o", "addr", "show", VRF_INTERFACE)
    assert "192.0.2.254/30" in addrs, addrs


@needs_vrf_fixture
def test_configure_and_remove_a_vrf_route_is_independently_verifiable():
    destination = _unique_test_prefix()

    d = Driver(_context())
    try:
        assert d.login()["success"] is True
        result = d.configure_route(
            destination=destination,
            next_hop=VRF_NEXT_HOP,
            interface=VRF_INTERFACE,
            virtual_router=VRF_NAME,
        )
        assert result["success"] is True, result

        # Channel 1: the kernel table the VRF maps to, read without FRR.
        kernel = _vrf_kernel_routes()
        assert destination in kernel, kernel
        # Channel 2: FRR's own per-VRF static view.
        frr_view = _vrf_frr_routes()
        assert destination in frr_view, frr_view
        # And it is NOT in the default table: a VRF route that leaked into the
        # default table is exactly the silent failure X-G exists to prevent.
        assert destination not in _show_ip_route_static(), (
            "the VRF route also landed in the default routing table"
        )

        removed = d.remove_route(
            destination=destination,
            next_hop=VRF_NEXT_HOP,
            interface=VRF_INTERFACE,
            virtual_router=VRF_NAME,
        )
        assert removed["success"] is True, removed
        assert destination not in _vrf_kernel_routes()
        assert destination not in _vrf_frr_routes()
    finally:
        d.logout()
        _cleanup_vrf_route(destination, VRF_NEXT_HOP)


@needs_vrf_fixture
def test_a_route_naming_an_unknown_vrf_reports_failure():
    """ADR 0014 addendum X-H: FRR ACCEPTS the route into its configuration (no
    "%" line at all) but never installs it, answering "Static Route to <prefix>
    not installed currently because dependent config not fully available". Under
    the rejection contract that is a failure, not a success."""
    destination = _unique_test_prefix()

    d = Driver(_context())
    try:
        assert d.login()["success"] is True
        result = d.configure_route(
            destination=destination,
            next_hop=VRF_NEXT_HOP,
            interface=VRF_INTERFACE,
            virtual_router=UNKNOWN_VRF_NAME,
        )
        assert result["success"] is False, (
            f"driver reported success for a route the device never installed: {result!r}"
        )
        assert "not installed currently" in result["error"], result

        # Independent verification: nothing landed in the real VRF's table or in
        # the default table either.
        assert destination not in _vrf_kernel_routes()
        assert destination not in _show_ip_route_static()
    finally:
        d.logout()
        _cleanup_vrf_route(destination, VRF_NEXT_HOP)
        subprocess.run(
            [
                "docker",
                "exec",
                FRR_CONTAINER,
                "vtysh",
                "-c",
                "configure terminal",
                "-c",
                f"no ip route {destination} {VRF_NEXT_HOP} vrf {UNKNOWN_VRF_NAME}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )


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
