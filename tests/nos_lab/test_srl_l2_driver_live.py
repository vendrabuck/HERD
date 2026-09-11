"""Live tests for the real Nokia SR Linux Layer 2 driver (drivers/srl_l2/driver.py)
against the checked-in emulated-gear test lab (infra/nos-test, docs/NOS_LAB.md).

Mirrors tests/nos_lab/test_nos_lab_live.py's gating exactly: skipped
automatically when the SR Linux node is not reachable, so the suite stays
green on CI and on any host that has not run `make nos-up`. Setting
HERD_TEST_NOS_REQUIRED=1 disables the skip and turns an unreachable node
into a hard failure instead, matching the same convention.

Every test instantiates the REAL Driver class (loaded straight from
drivers/srl_l2/driver.py, not a copy) and verifies its changes
independently: a separate `docker exec nos-test-srl sr_cli ...` call, never
through the driver's own netmiko session or its status() method. A driver
that lies consistently must not be able to pass its own test. Each test
picks a random, per-run-unique VLAN id so concurrent or repeated runs
(including the sibling tests/nos_lab/test_nos_lab_live.py suite, which
picks from random.randint(100, 4000) for its own throwaway SR Linux VLAN)
do not collide, and cleans up everything it creates, including on
assertion failure, so the lab stays re-runnable without a reset.

One test drives a genuine device rejection (an out-of-range vlan id) and
proves BOTH halves of the contract: the driver reports success: False
rather than a false success or a raised exception, AND the independent
docker exec read confirms nothing was actually created on the device. HERD
keys provisioning success on the driver's returned payload, so the first
half without the second would not catch a driver that returns the right
shape while still leaving stray state behind.
"""

from __future__ import annotations

import importlib.util
import os
import random
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_PATH = _REPO_ROOT / "drivers" / "srl_l2" / "driver.py"
_spec = importlib.util.spec_from_file_location("srl_l2_driver_live", _DRIVER_PATH)
srl_driver = importlib.util.module_from_spec(_spec)
sys.modules["srl_l2_driver_live"] = srl_driver
_spec.loader.exec_module(srl_driver)

Driver = srl_driver.Driver

SRL_HOST = os.getenv("HERD_TEST_SRL_HOST", "127.0.0.1")
SRL_PORT = int(os.getenv("HERD_TEST_SRL_PORT", "2223"))
SRL_USERNAME = "admin"
SRL_PASSWORD = "NokiaSrl1!"
SRL_CONTAINER = "nos-test-srl"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_SRL_REACHABLE = _reachable(SRL_HOST, SRL_PORT)
_NOS_REQUIRED = os.getenv("HERD_TEST_NOS_REQUIRED", "") not in ("", "0")

pytestmark = pytest.mark.skipif(
    not _NOS_REQUIRED and not _SRL_REACHABLE,
    reason=(
        f"NOS test lab SR Linux node not reachable ({SRL_HOST}:{SRL_PORT}); "
        "start it with `make nos-up` to run."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _NOS_REQUIRED and not _SRL_REACHABLE:
        pytest.fail(
            f"HERD_TEST_NOS_REQUIRED is set but the SR Linux node is not reachable "
            f"({SRL_HOST}:{SRL_PORT}); run `make nos-up` (infra/nos-test) or unset "
            "HERD_TEST_NOS_REQUIRED."
        )


def _docker_exec(*args: str) -> str:
    """Run a command inside the SR Linux lab container and return its stdout.

    This is the independent verification channel: it never goes through the
    netmiko session the driver used, so it proves the device's own state,
    not just that the driver's session believes it succeeded.

    No "-c" flag on sr_cli calls: sr_cli's "-c" means "--commit-at-end", not
    "run this command"; a bare positional argument is the correct
    read-only, non-interactive form (docs/NOS_LAB.md).
    """
    result = subprocess.run(
        ["docker", "exec", SRL_CONTAINER, *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"docker exec {SRL_CONTAINER} {' '.join(args)} failed (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout


def _info(path: str) -> str:
    return _docker_exec("sr_cli", f"info {path}")


def _random_vlan_id(low: int, high: int) -> int:
    return random.randint(low, high)


def _context() -> dict:
    return {
        "HERD_ip": SRL_HOST,
        "HERD_login": SRL_USERNAME,
        "HERD_password": SRL_PASSWORD,
        "HERD_port": SRL_PORT,
    }


# ---------------------------------------------------------------------------
# Full lifecycle, tagged: create, add, verify mac-vrf + binding + subinterface
# independently, prove create_vlan idempotency, remove and delete, verify gone.
# ---------------------------------------------------------------------------


def test_create_add_tagged_verify_idempotent_recreate_remove_and_delete():
    vlan_id = _random_vlan_id(2000, 2999)
    net_instance = f"vlan{vlan_id}"
    port = "ethernet-1/1"
    subif_path = f"/interface {port} subinterface {vlan_id}"
    net_path = f"/network-instance {net_instance}"

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        assert driver.create_vlan(vlan_id)["success"] is True
        assert driver.add_to_vlan(port=port, vlan_id=vlan_id, tag="tagged")["success"] is True

        # Independent verification: the mac-vrf exists AND the subinterface is
        # bound into it (binding is the part most likely to be silently
        # skipped), plus the subinterface itself is the right shape.
        net_info = _info(net_path)
        assert "type mac-vrf" in net_info, net_info
        assert f"interface {port}.{vlan_id}" in net_info, net_info
        subif_info = _info(subif_path)
        assert "type bridged" in subif_info, subif_info
        assert "single-tagged" in subif_info, subif_info
        assert f"vlan-id {vlan_id}" in subif_info, subif_info

        # create_vlan MUST be idempotent: redefining it a second time is a
        # success, not an error, and leaves the same state behind.
        assert driver.create_vlan(vlan_id)["success"] is True
        net_info_again = _info(net_path)
        assert "type mac-vrf" in net_info_again, net_info_again
        assert f"interface {port}.{vlan_id}" in net_info_again, net_info_again
    finally:
        # Cleanup runs even on assertion failure, so the lab stays re-runnable.
        assert driver.remove_from_vlan(port=port, vlan_id=vlan_id)["success"] is True
        assert driver.delete_vlan(vlan_id)["success"] is True
        driver.logout()

    # Independent verification that both the binding/subinterface and the
    # network-instance itself are gone.
    assert _info(subif_path).strip() == "", _info(subif_path)
    assert _info(net_path).strip() == "", _info(net_path)


# ---------------------------------------------------------------------------
# Untagged: only the tagged case was hand-verified before this driver was
# written, per the brief; this proves the untagged encap form live too.
# ---------------------------------------------------------------------------


def test_add_to_vlan_untagged_is_independently_verifiable():
    vlan_id = _random_vlan_id(3000, 3999)
    net_instance = f"vlan{vlan_id}"
    port = "ethernet-1/2"
    subif_path = f"/interface {port} subinterface {vlan_id}"
    net_path = f"/network-instance {net_instance}"

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        assert driver.create_vlan(vlan_id)["success"] is True
        assert driver.add_to_vlan(port=port, vlan_id=vlan_id, tag="untagged")["success"] is True

        net_info = _info(net_path)
        assert "type mac-vrf" in net_info, net_info
        assert f"interface {port}.{vlan_id}" in net_info, net_info
        subif_info = _info(subif_path)
        assert "type bridged" in subif_info, subif_info
        assert "untagged" in subif_info, subif_info
        # The untagged form must not also carry a single-tagged vlan-id.
        assert "single-tagged" not in subif_info, subif_info
    finally:
        assert driver.remove_from_vlan(port=port, vlan_id=vlan_id)["success"] is True
        assert driver.delete_vlan(vlan_id)["success"] is True
        driver.logout()

    assert _info(subif_path).strip() == "", _info(subif_path)
    assert _info(net_path).strip() == "", _info(net_path)


# ---------------------------------------------------------------------------
# delete_vlan on a VLAN that was never created must also succeed.
# ---------------------------------------------------------------------------


def test_delete_vlan_on_missing_vlan_is_idempotent():
    vlan_id = _random_vlan_id(3500, 3999)
    net_instance = f"vlan{vlan_id}"
    net_path = f"/network-instance {net_instance}"

    # Confirm it genuinely does not exist before calling delete on it.
    assert _info(net_path).strip() == "", _info(net_path)

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        result = driver.delete_vlan(vlan_id)
        assert result["success"] is True
    finally:
        driver.logout()

    # Still absent; the call was a genuine no-op, not a partial create.
    assert _info(net_path).strip() == "", _info(net_path)


# ---------------------------------------------------------------------------
# A device rejection must surface as success: False, not a false success and
# not a raised exception, and must leave nothing on the device. HERD keys
# provisioning success on this returned payload, so a false success here
# would make HERD record an ACTIVE VLAN membership the switch never actually
# accepted (execution_service.py's driver_result_failed helper).
# ---------------------------------------------------------------------------


def test_add_to_vlan_with_out_of_range_vlan_id_fails_and_creates_nothing():
    # A vlan_id in this range is a VALID subinterface index (0..9999) but
    # always exceeds the encap vlan-id's valid range (1..4094), so it
    # reliably reproduces the same rejection every run while still picking a
    # fresh id per run.
    vlan_id = _random_vlan_id(4095, 9999)
    net_instance = f"vlan{vlan_id}"
    port = "ethernet-1/1"
    subif_path = f"/interface {port} subinterface {vlan_id}"
    net_path = f"/network-instance {net_instance}"

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        result = driver.add_to_vlan(port=port, vlan_id=vlan_id, tag="tagged")
        assert result["success"] is False
        assert result.get("error"), result
    finally:
        driver.logout()

    # Independent verification: a fresh docker exec, never the driver's own
    # session. A rejected batch must leave the running config untouched, even
    # though some sibling lines in the same batch (vlan-tagging true, the
    # bridged subinterface itself) parse fine on their own and would
    # otherwise have staged into the candidate.
    assert _info(subif_path).strip() == "", _info(subif_path)
    assert _info(net_path).strip() == "", _info(net_path)


# ---------------------------------------------------------------------------
# status() reachability, independent of the mutating-op tests above.
# ---------------------------------------------------------------------------


def test_status_reports_reachable_against_the_real_node():
    driver = Driver(_context())
    result = driver.status()
    assert result["reachable"] is True
