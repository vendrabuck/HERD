"""Live tests against the checked-in emulated-gear test lab (infra/nos-test,
docs/NOS_LAB.md): a Nokia SR Linux node (Layer 2) and an FRRouting node
(Layer 3 / Cisco dialect over vtysh).

Skipped automatically when neither node is reachable, so the suite stays
green on CI and on any host that has not run `make nos-up`. Setting
HERD_TEST_NOS_REQUIRED=1 disables the skip, mirroring
services/auth/tests/test_ldap_service_live.py's HERD_TEST_LDAP_REQUIRED
convention: an unreachable lab then FAILS every test with an explicit
remedy instead of silently skipping.

Each test uses a unique per-run identifier (a random VLAN id, a random
RFC5737 TEST-NET-1 prefix) so concurrent or repeated runs never collide,
verifies its change independently of the netmiko session that made it (a
separate `docker exec ... sr_cli`/`vtysh` call, never the driver's own
status method), and cleans up afterward so the lab stays re-runnable
without a reset.
"""

from __future__ import annotations

import ipaddress
import os
import random
import re
import socket
import subprocess

import pytest
from netmiko import ConnectHandler

SRL_HOST = os.getenv("HERD_TEST_SRL_HOST", "127.0.0.1")
SRL_PORT = int(os.getenv("HERD_TEST_SRL_PORT", "2223"))
SRL_USERNAME = "admin"
SRL_PASSWORD = "NokiaSrl1!"
SRL_CONTAINER = "nos-test-srl"

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


_SRL_REACHABLE = _reachable(SRL_HOST, SRL_PORT)
_FRR_REACHABLE = _reachable(FRR_HOST, FRR_PORT)
_NOS_REACHABLE = _SRL_REACHABLE and _FRR_REACHABLE
_NOS_REQUIRED = os.getenv("HERD_TEST_NOS_REQUIRED", "") not in ("", "0")

pytestmark = pytest.mark.skipif(
    not _NOS_REQUIRED and not _NOS_REACHABLE,
    reason=(
        f"NOS test lab not fully reachable (srl {SRL_HOST}:{SRL_PORT} "
        f"reachable={_SRL_REACHABLE}, frr {FRR_HOST}:{FRR_PORT} "
        f"reachable={_FRR_REACHABLE}); start it with `make nos-up` to run."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _NOS_REQUIRED and not _NOS_REACHABLE:
        pytest.fail(
            f"HERD_TEST_NOS_REQUIRED is set but the NOS test lab is not fully "
            f"reachable (srl reachable={_SRL_REACHABLE}, frr reachable={_FRR_REACHABLE}); "
            "run `make nos-up` (infra/nos-test) or unset HERD_TEST_NOS_REQUIRED."
        )


def _docker_exec(container: str, *args: str) -> str:
    """Run a command inside a lab container and return its stdout.

    This is the independent verification channel: it never goes through the
    netmiko session that made the change, so it proves the device's own
    state, not just that the driver's session believes it succeeded.
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


def _srl_connection() -> ConnectHandler:
    return ConnectHandler(
        device_type="nokia_srl",
        host=SRL_HOST,
        port=SRL_PORT,
        username=SRL_USERNAME,
        password=SRL_PASSWORD,
        fast_cli=False,
    )


def _frr_connection() -> ConnectHandler:
    return ConnectHandler(
        device_type="cisco_ios",
        host=FRR_HOST,
        port=FRR_PORT,
        username=FRR_USERNAME,
        password=FRR_PASSWORD,
        fast_cli=False,
    )


# ---------------------------------------------------------------------------
# SR Linux (Layer 2): create a mac-vrf network-instance bound to a bridged
# subinterface, verify independently via sr_cli inside the container, then
# clean up.
# ---------------------------------------------------------------------------


def test_srl_create_vlan_binds_interface_and_is_independently_verifiable():
    vlan_id = random.randint(100, 4000)
    net_instance = f"herd-test-vlan{vlan_id}"
    subif = f"ethernet-1/1.{vlan_id}"

    conn = _srl_connection()
    try:
        conn.send_config_set(
            [
                "interface ethernet-1/1 vlan-tagging true",
                f"interface ethernet-1/1 subinterface {vlan_id} type bridged",
                (
                    f"interface ethernet-1/1 subinterface {vlan_id} vlan encap "
                    f"single-tagged vlan-id {vlan_id}"
                ),
                f"network-instance {net_instance} type mac-vrf",
                f"network-instance {net_instance} interface {subif}",
            ]
        )
        commit_output = conn.commit()
        assert "error" not in commit_output.lower(), commit_output
    finally:
        conn.disconnect()

    try:
        # Independent verification: a fresh docker exec into the container,
        # never the netmiko session that made the change.
        # No "-c" flag: sr_cli's "-c" means "--commit-at-end", not "run this
        # command"; a bare positional argument is the correct read-only,
        # non-interactive form (see infra/nos-test/srl/start.sh's comment).
        info = _docker_exec(SRL_CONTAINER, "sr_cli", f"info /network-instance {net_instance}")
        assert "mac-vrf" in info, info
        assert subif in info, info
    finally:
        # Cleanup so the lab is re-runnable without a reset.
        cleanup_conn = _srl_connection()
        try:
            cleanup_conn.send_config_set(
                [
                    f"delete network-instance {net_instance}",
                    f"delete interface ethernet-1/1 subinterface {vlan_id}",
                ]
            )
            cleanup_conn.commit()
        finally:
            cleanup_conn.disconnect()


# ---------------------------------------------------------------------------
# FRR (Layer 3 / Cisco dialect): add a static route to a unique RFC5737
# TEST-NET-1 (192.0.2.0/24) prefix, verify independently via vtysh, remove
# it, and confirm it is gone.
#
# The DESTINATION prefix is the random, per-run-unique RFC5737 address; the
# NEXTHOP must instead be reachable through the container's own connected
# subnet, or FRR leaves the route unresolved and it never becomes SELECTED
# ('>' in the RIB) or shows up under `show ip route static` at all (verified
# live: a route toward an unreachable RFC5737 nexthop is silently absent
# from both `show ip route` and `show ip route static`, not merely
# unselected). The nexthop is resolved dynamically from the container's own
# `eth0` address rather than hardcoded, since docker assigns this compose
# project's subnet at network-creation time.
# ---------------------------------------------------------------------------


def _unique_test_prefix() -> str:
    """Return a random /30 destination prefix inside RFC5737 TEST-NET-1
    (192.0.2.0/24) so concurrent or repeated runs do not collide on the
    destination. 192.0.2.0/24 has 64 non-overlapping /30s."""
    network = ipaddress.ip_network("192.0.2.0/24")
    subnets = list(network.subnets(new_prefix=30))
    subnet = random.choice(subnets)
    return f"{subnet.network_address}/{subnet.prefixlen}"


def _frr_connected_nexthop() -> str:
    """Return an address on the FRR container's own connected subnet, so a
    static route toward it resolves and is SELECTED rather than sitting
    unresolved. Sharing this nexthop across concurrent runs is harmless:
    only the destination prefix needs to be unique."""
    addr_output = _docker_exec(FRR_CONTAINER, "ip", "-4", "-o", "addr", "show", "eth0")
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr_output)
    assert match, f"could not parse eth0 address from: {addr_output!r}"
    iface = ipaddress.ip_interface(match.group(1))
    candidate = iface.network.broadcast_address - 1
    if candidate == iface.ip:
        candidate = iface.network.broadcast_address - 2
    return str(candidate)


def test_frr_add_and_remove_static_route_is_independently_verifiable():
    prefix = _unique_test_prefix()
    nexthop = _frr_connected_nexthop()

    conn = _frr_connection()
    try:
        output = conn.send_config_set([f"ip route {prefix} {nexthop}"])
        assert "invalid" not in output.lower(), output
    finally:
        conn.disconnect()

    try:
        # Independent verification: a fresh vtysh call inside the container,
        # never the netmiko session that made the change.
        routes = _docker_exec(FRR_CONTAINER, "vtysh", "-c", "show ip route static")
        assert prefix in routes, routes
        # FRR marks the selected/installed route with '*' in `show ip route`.
        show_all = _docker_exec(FRR_CONTAINER, "vtysh", "-c", "show ip route")
        assert prefix in show_all, show_all
    finally:
        cleanup_conn = _frr_connection()
        try:
            cleanup_conn.send_config_set([f"no ip route {prefix} {nexthop}"])
        finally:
            cleanup_conn.disconnect()

    routes_after = _docker_exec(FRR_CONTAINER, "vtysh", "-c", "show ip route static")
    assert prefix not in routes_after, routes_after
