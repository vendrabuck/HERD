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


# ---------------------------------------------------------------------------
# Issue #778: candidate discipline.
#
# SR Linux stages every set/delete in a per-user PRIVATE CANDIDATE that only
# `commit stay` applies, and that candidate outlives the SSH session it was
# staged in. netmiko also keeps sending after a rejected line. Together those
# two facts produced the bug: a commit issued before the set output was judged
# applied the valid PREFIX of a batch the driver then reported as failed, and
# a candidate left dirty by a call that died rode into the next call's commit
# through the DEVICE (the sandbox runs one process per action, so there is no
# shared session to blame).
#
# These tests run on ethernet-1/3, which the lab baseline deliberately does
# NOT touch (ethernet-1/1 and ethernet-1/2 already carry `vlan-tagging true`,
# which would mask exactly the partial apply under test). Independent
# verification is a separate `docker exec nos-test-srl sr_cli` read, never the
# driver's own session.
# ---------------------------------------------------------------------------

_UNTOUCHED_PORT = "ethernet-1/3"
_UNTOUCHED_PORT_PATH = f"/interface {_UNTOUCHED_PORT}"


def _restore_untouched_port():
    """Return ethernet-1/3 to its baseline (absent from the running config).

    Runs through a separate sr_cli session rather than the driver, so a driver
    bug cannot quietly skip its own cleanup. `delete` on an absent path is a
    clean no-op on SR Linux (verified live), so this is safe to call
    unconditionally.
    """
    _docker_exec(
        "bash",
        "-c",
        "sr_cli <<'EOS'\n"
        "enter candidate private\n"
        f"delete {_UNTOUCHED_PORT_PATH}\n"
        "commit stay\n"
        "quit\n"
        "EOS\n",
    )


def _stage_stale_candidate(line):
    """Stage `line` in the admin user's private candidate and walk away.

    Deliberately a second netmiko SSH session as the SAME user, not a
    `docker exec sr_cli` session: verified live, the container-side sr_cli
    runs as a different user and gets a DIFFERENT private candidate, which
    the driver's admin session cannot see. Only a same-user session
    reproduces the leak this test is about.

    Disconnects without committing and without discarding, which is what a
    driver process killed by the sandbox rlimit leaves behind.
    """
    from netmiko import ConnectHandler

    conn = ConnectHandler(
        device_type="nokia_srl",
        host=SRL_HOST,
        username=SRL_USERNAME,
        password=SRL_PASSWORD,
        port=SRL_PORT,
        fast_cli=False,
    )
    try:
        conn.config_mode()
        conn.send_config_set([line])
        diff = conn.send_command("diff")
        assert diff.strip(), "precondition failed: nothing staged in the private candidate"
    finally:
        conn.disconnect()


def _clear_admin_candidate():
    """Discard whatever is left in the admin user's private candidate."""
    from netmiko import ConnectHandler

    conn = ConnectHandler(
        device_type="nokia_srl",
        host=SRL_HOST,
        username=SRL_USERNAME,
        password=SRL_PASSWORD,
        port=SRL_PORT,
        fast_cli=False,
    )
    try:
        conn.config_mode()
        conn._discard()
    finally:
        conn.disconnect()


def test_rejected_batch_commits_nothing_of_its_valid_prefix():
    """The literal replay of issue #778's first transcript.

    Two lines, the first valid and the second a parsing error. netmiko sends
    both; before the fix the unconditional commit() answered "All changes have
    been committed." and `admin-state enable` landed on a port HERD believed
    untouched, while the caller was told success: False.

    This drives _apply directly rather than a contract method because no
    contract method can BUILD a syntactically invalid line: the partial-apply
    window is a property of the batch, and this is the batch that opens it.
    """
    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        result = driver._apply(
            [
                f"set / interface {_UNTOUCHED_PORT} admin-state enable",
                f"set / interface {_UNTOUCHED_PORT} vlan-taggingX true",
            ]
        )
        assert result["success"] is False, result
        assert "Parsing error:" in result["error"], result
        # Read the device BEFORE cleanup. Asserting after the finally block
        # would assert on the cleanup's work, not the driver's: the first cut
        # of this test did exactly that and passed against the unfixed driver.
        observed = _info(_UNTOUCHED_PORT_PATH)
    finally:
        driver.logout()
        _restore_untouched_port()

    # The whole point: the valid first line must NOT be in the running config.
    assert observed.strip() == "", observed


def test_add_to_vlan_rejection_leaves_an_untouched_port_untouched():
    """The same rule through the public contract method, on a port whose
    baseline carries nothing.

    add_to_vlan's first line is `vlan-tagging true`, which parses fine on its
    own; an out-of-range vlan id only rejects further down the batch. The
    sibling out-of-range test above runs on ethernet-1/1, whose baseline
    already sets vlan-tagging, so it cannot see a leaked flag. This one can.

    Honest scope note: this test does NOT fail against the unfixed driver, and
    is not the regression detector for #778
    (test_rejected_batch_commits_nothing_of_its_valid_prefix is). Measured
    live: this batch's valid prefix is semantically inconsistent on its own
    (vlan-tagging plus an out-of-range encap), so SR Linux refuses the whole
    commit and the partial apply never opens. It is kept as the public-contract
    guard on a port with no baseline config, where a future regression that
    DOES leak through add_to_vlan would show up.
    """
    vlan_id = _random_vlan_id(4095, 9999)
    net_path = f"/network-instance vlan{vlan_id}"

    assert _info(_UNTOUCHED_PORT_PATH).strip() == "", "precondition: port must start clean"

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        result = driver.add_to_vlan(port=_UNTOUCHED_PORT, vlan_id=vlan_id, tag="tagged")
        assert result["success"] is False, result
        assert result.get("error"), result
        observed_port = _info(_UNTOUCHED_PORT_PATH)  # before cleanup; see the test above
        observed_net = _info(net_path)
    finally:
        driver.logout()
        _restore_untouched_port()

    assert observed_port.strip() == "", observed_port
    assert observed_net.strip() == "", observed_net


def test_a_stale_candidate_from_an_earlier_call_is_never_committed():
    """The cross-session half of issue #778.

    A previous action that died after staging leaves lines in the admin
    user's private candidate ON THE DEVICE. A fresh sandbox process is no
    protection: its `enter candidate private` lands in that same candidate,
    and its commit would apply the orphaned lines alongside its own. The entry
    discard in _apply is what stops that.
    """
    vlan_id = _random_vlan_id(2000, 2999)
    net_path = f"/network-instance vlan{vlan_id}"
    stale = f"set / interface {_UNTOUCHED_PORT} description HERD-778-STALE"

    assert _info(_UNTOUCHED_PORT_PATH).strip() == "", "precondition: port must start clean"
    _stage_stale_candidate(stale)

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        # An ordinary, entirely valid call. It must commit its OWN work only.
        assert driver.create_vlan(vlan_id)["success"] is True
        # Before cleanup; see test_rejected_batch_commits_nothing_of_its_valid_prefix.
        port_info = _info(_UNTOUCHED_PORT_PATH)
        # The call's own work DID land, so a driver that simply does nothing
        # cannot pass this test by accident.
        assert "type mac-vrf" in _info(net_path), _info(net_path)
    finally:
        driver.delete_vlan(vlan_id)
        driver.logout()
        _clear_admin_candidate()
        _restore_untouched_port()

    assert "HERD-778-STALE" not in port_info, port_info
    assert port_info.strip() == "", port_info
    assert _info(net_path).strip() == "", _info(net_path)


def test_remove_from_vlan_on_an_absent_membership_is_idempotent():
    """`delete` on an absent path is a clean no-op on SR Linux, so a redelivered
    or retried release converges instead of failing. Verified live rather than
    assumed, the same way create_vlan/delete_vlan idempotency is."""
    vlan_id = _random_vlan_id(2000, 2999)
    port = "ethernet-1/1"
    subif_path = f"/interface {port} subinterface {vlan_id}"
    net_path = f"/network-instance vlan{vlan_id}"

    # Confirm there is genuinely nothing to remove before calling remove.
    assert _info(subif_path).strip() == "", _info(subif_path)
    assert _info(net_path).strip() == "", _info(net_path)

    driver = Driver(_context())
    assert driver.login()["success"] is True
    try:
        result = driver.remove_from_vlan(port=port, vlan_id=vlan_id)
        assert result["success"] is True, result
    finally:
        driver.logout()

    # Still absent, and the call created nothing on its way through.
    assert _info(subif_path).strip() == "", _info(subif_path)
    assert _info(net_path).strip() == "", _info(net_path)
