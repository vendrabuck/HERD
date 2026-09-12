"""End-to-end proof: HERD's own API derives a Layer 2 VLAN membership from a
reservation's wiring and configures it on a REAL Nokia SR Linux switch through
HERD's execution service and driver sandbox (drivers/srl_l2), against the
checked-in NOS test lab (infra/nos-test, docs/NOS_LAB.md). This is phase 3b of
the emulated-gear test tier (ADR 0010), the Layer 2 counterpart of
test_frr_l3_via_stack_live.py's Layer 3 proof.

Why this test reuses the SEEDED lab devices rather than creating its own
(unlike test_frr_l3_via_stack_live.py, which uploads a throwaway driver and
device): L2 membership is derived from RECORDED HOPS (ADR 0009), which in turn
come from cabling's pathfinder walking the PHYSICAL connections graph. That
graph only exists between `nos-lab-dut-1`, `nos-lab-dut-2`, and `nos-lab-srl`
because `make seed-nos` cabled them: `nos-lab-dut-1:eth1` to
`nos-lab-srl:ethernet-1/1`, `nos-lab-dut-2:eth1` to `nos-lab-srl:ethernet-1/2`.
A throwaway device would have no physical path to the real switch to resolve
through. So this test looks the three devices up by name (seeded ahead of
time; `make seed-nos` is idempotent) and creates only the topology
and reservation on top of them.

The load-bearing rule under test (docs/design/0009-l2-l3-connection-driven-
reconcile.md): L2 VLAN membership is derived from the fork's RECORDED HOPS,
not from per-hop deltas, and it ALWAYS full-reconciles against cabling's
intended set on a `reservation.wiring_changed` event. A canvas edge directly
between the two DUTs (no switch node) is intentional: cabling's pathfinder
resolves it through the physical cabling graph into two hops touching
`ethernet-1/1` and `ethernet-1/2`, exactly mirroring
tests/integration/test_l2_reconcile.py's canvas shape against the mock L2
driver, but here against the real switch.

The reservation deliberately ACTIVATES over an EDGELESS canvas, and the edge
arrives in a later fork SAVE. That ordering is what makes the save's effect
observable: with nothing wired at activation the switch carries no membership
at all, so the mac-vrf and both subinterface bindings that appear afterwards
can only have come from the save's connection-driven reconcile. Re-saving the
same canvas the reservation activated over (the shape this test used to have)
proves nothing, since activation had already driven that exact set.

Every change is verified independently of the driver's own session, the
execution run's own success flag, AND HERD's own read of the wiring-status
surface: a separate `docker exec nos-test-srl sr_cli ...` call, in its own
process, never trusted implicitly. HERD's wiring-status ledger is polled
first (to learn the VLAN id HERD allocated; that number cannot be assumed) and
then cross-checked against the device's own answer, at both provision and
teardown, so the test also proves HERD's ledger and the device agree, not
just that the device eventually converges.

Placement and gating mirror test_frr_l3_via_stack_live.py exactly: this needs
BOTH the NOS test lab (`make nos-up`) and a running dev stack (`make up`) with
the lab attached (`make nos-attach`), lives under tests/nos_lab/ (never
invoked by `make test`, `make master`, or `make everything`), and
`HERD_TEST_NOS_REQUIRED=1` turns a missing precondition into a hard failure
instead of a skip.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

SRL_HOST = os.getenv("HERD_TEST_SRL_HOST", "127.0.0.1")
SRL_PORT = int(os.getenv("HERD_TEST_SRL_PORT", "2223"))
SRL_CONTAINER = "nos-test-srl"

BASE_URL = os.getenv("HERD_BASE_URL", "https://localhost/api")
SEED_EMAIL = os.getenv("SEED_EMAIL") or os.getenv("SUPERADMIN_EMAIL", "admin@example.com")
SEED_PASSWORD = os.getenv("SEED_PASSWORD") or os.getenv("SUPERADMIN_PASSWORD", "admin123!")

DUT_1_NAME = "nos-lab-dut-1"
DUT_2_NAME = "nos-lab-dut-2"
SRL_NAME = "nos-lab-srl"
SRL_PORT_1 = "ethernet-1/1"
SRL_PORT_2 = "ethernet-1/2"


# ---------------------------------------------------------------------------
# Preconditions: the lab reachable, the lab ATTACHED to the stack's network,
# the stack itself reachable, and the credentials accepted. Skipped by
# default; HERD_TEST_NOS_REQUIRED=1 turns any missing precondition into a
# hard failure.
# ---------------------------------------------------------------------------


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _lab_attached_to_stack() -> bool:
    """True iff nos-test-srl can resolve a HERD service by Docker DNS, proving
    it is attached to the stack's Docker network (`make nos-attach`)."""
    try:
        result = subprocess.run(
            ["docker", "exec", SRL_CONTAINER, "getent", "hosts", "execution"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _stack_reachable() -> bool:
    try:
        with httpx.Client(verify=False, timeout=5.0) as c:
            c.post(
                f"{BASE_URL}/auth/login",
                json={"email": "unreachable@example.invalid", "password": "x"},
            )
        return True
    except httpx.HTTPError:
        return False


_NOS_REQUIRED = os.getenv("HERD_TEST_NOS_REQUIRED", "") not in ("", "0")


def _credentials_accepted() -> bool:
    """Whether SEED_EMAIL/SUPERADMIN_EMAIL actually authenticate against the stack.

    Resolved from the ENVIRONMENT, not from .env: the stack seeds its
    superadmin from .env, so a shell that has not exported those values falls
    back to the generic default and gets a bare 401. Probing here turns that
    into a precondition message naming the fix instead.
    """
    try:
        with httpx.Client(verify=False, timeout=10) as client:
            resp = client.post(
                f"{BASE_URL}/auth/login",
                json={"email": SEED_EMAIL, "password": SEED_PASSWORD},
            )
        return resp.status_code == 200
    except Exception:
        return False


def _missing_precondition_reason() -> str:
    """Probe every precondition in dependency order and return the first
    unmet one's message, or "" when all hold.

    Called from a session-scoped fixture, NEVER at import: the repo-root
    pytest config sets testpaths = ["tests"], so a bare `uv run pytest` from
    the repo root collects this file, and an import-time probe would make
    plain collection open a socket, shell out to docker, and log in over
    HTTPS. The dependency order (and each message) is unchanged; it is only
    the timing that moved.
    """
    if not _reachable(SRL_HOST, SRL_PORT):
        return (
            f"NOS test lab SR Linux node not reachable ({SRL_HOST}:{SRL_PORT}); "
            "start it with `make nos-up`."
        )
    if not _lab_attached_to_stack():
        return (
            "NOS test lab is not attached to the dev stack's Docker network; "
            "run `make nos-attach` (dev stack must be up: `make up`)."
        )
    if not _stack_reachable():
        return f"HERD stack not reachable at {BASE_URL}; run `make up`."
    if not _credentials_accepted():
        return (
            f"the stack rejected the seed credentials for {SEED_EMAIL!r}. These are read "
            "from the ENVIRONMENT, while the stack seeds its superadmin from .env, so "
            "export them first, for example: "
            "export SUPERADMIN_EMAIL=$(grep -E '^SUPERADMIN_EMAIL=' .env | cut -d= -f2-) "
            "and the same for SUPERADMIN_PASSWORD; or set SEED_EMAIL/SEED_PASSWORD."
        )
    return ""


@pytest.fixture(scope="session")
def _nos_precondition_reason() -> str:
    """The one probe pass for the whole session (each probe is a socket, a
    docker exec, and two HTTPS logins; running them per test would triple
    that for no added signal)."""
    return _missing_precondition_reason()


@pytest.fixture(autouse=True)
def _require_nos_lab_and_stack(_nos_precondition_reason: str) -> None:
    """Unchanged gating semantics: skip by default, hard-fail (with the same
    message) under HERD_TEST_NOS_REQUIRED=1."""
    if not _nos_precondition_reason:
        return
    if _NOS_REQUIRED:
        pytest.fail(_nos_precondition_reason)
    pytest.skip(_nos_precondition_reason)


# ---------------------------------------------------------------------------
# Independent, driver-session-free verification against the real device.
# ---------------------------------------------------------------------------


def _docker_exec(*args: str) -> str:
    """Run a command inside the SR Linux lab container and return its stdout.

    No "-c" flag on sr_cli calls: sr_cli's "-c" means "--commit-at-end", not
    "run this command"; a bare positional argument is the correct read-only,
    non-interactive form (docs/NOS_LAB.md). This is a fresh subprocess every
    call, never the driver's own netmiko session.
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


def _network_instance_info(vlan_id: int) -> str:
    return _info(f"/network-instance vlan{vlan_id}")


def _subinterface_info(port: str, vlan_id: int) -> str:
    return _info(f"/interface {port} subinterface {vlan_id}")


def _assert_membership_on_device(vlan_id: int, ports: list[str]) -> None:
    """The mac-vrf network-instance exists AND both subinterfaces are bound
    into it (the binding is the part most likely to be silently skipped, so
    it is asserted explicitly, not inferred from the network-instance alone),
    plus each subinterface itself carries the expected bridged/single-tagged
    shape execution always requests (tag="tagged" is hardcoded at the
    nats_consumer.py call site)."""
    net_info = _network_instance_info(vlan_id)
    assert "type mac-vrf" in net_info, net_info
    for port in ports:
        assert f"interface {port}.{vlan_id}" in net_info, net_info
        subif_info = _subinterface_info(port, vlan_id)
        assert "type bridged" in subif_info, subif_info
        assert "single-tagged" in subif_info, subif_info
        assert f"vlan-id {vlan_id}" in subif_info, subif_info


def _assert_ports_carry_no_subinterfaces(ports: list[str]) -> None:
    """Baseline, SCOPED to the ports this test will use.

    Deliberately not "no mac-vrf anywhere on the device": an unrelated mac-vrf
    (a leftover from the dialect suite, or a second lane) says nothing about
    whether ethernet-1/1 and ethernet-1/2 are free, and asserting on the whole
    device turns someone else's leftover into a misleading failure of this
    test. A port with no `subinterface` stanza cannot be a member of any
    mac-vrf, which is exactly the precondition that matters here.
    """
    for port in ports:
        port_info = _info(f"/interface {port}")
        assert "subinterface" not in port_info, (
            f"{port} already carries a subinterface before this test wired anything; "
            f"the lab is not at baseline:\n{port_info}"
        )


def _remove_membership_from_device(vlan_id: int, ports: list[str]) -> str | None:
    """Drop any leftover membership for `vlan_id` straight off the switch.

    Piped-script form, mirroring how the lab's own baseline is applied
    (infra/nos-test/srl/start.sh): sr_cli's "-c" flag means "--commit-at-end",
    not "run this command", so a batch of deletes plus an explicit
    "commit stay" is sent on stdin, never via "-c".

    Returns None on success, or a message naming the exit status and stderr.
    A silently failed cleanup does not stay silent: it resurfaces as the NEXT
    run's baseline failure, blamed on the wrong test.
    """
    net_path = f"/network-instance vlan{vlan_id}"
    script_lines = ["enter candidate"]
    for port in ports:
        script_lines.append(f"delete {net_path} interface {port}.{vlan_id}")
        script_lines.append(f"delete /interface {port} subinterface {vlan_id}")
    script_lines.append(f"delete {net_path}")
    script_lines.append("commit stay")
    result = subprocess.run(
        ["docker", "exec", "-i", SRL_CONTAINER, "sr_cli"],
        input="\n".join(script_lines) + "\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return (
            f"device cleanup for vlan{vlan_id} failed (exit {result.returncode}); "
            f"the lab is NOT back at baseline. stderr={result.stderr!r} "
            f"stdout={result.stdout!r}"
        )
    return None


def _assert_membership_gone_from_device(vlan_id: int, ports: list[str]) -> None:
    for port in ports:
        assert _subinterface_info(port, vlan_id).strip() == "", (
            f"subinterface {port}.{vlan_id} still present after teardown"
        )
    assert _network_instance_info(vlan_id).strip() == "", (
        f"network-instance vlan{vlan_id} still present after teardown"
    )


async def _poll_membership_gone_from_device(
    vlan_id: int, ports: list[str], timeout: float = 30.0
) -> None:
    """Poll the real device (never a session, never HERD's ledger) until both
    subinterface bindings and the mac-vrf itself are gone.

    delete_vlan runs in its OWN driver session, AFTER the whole membership
    remove pass and after HERD's l2_port_assignments rows already flip
    RELEASED (`_apply_l2_memberships`'s allocation-lifecycle coupling,
    `_release_orphaned_allocations`, ADR 0009 issue #442): so the ledger
    reaching RELEASED does not itself prove the VLAN definition is gone yet.
    delete_vlan is documented as log-and-continue best-effort (an empty
    definition may acceptably linger on a driver failure), so this polls
    rather than asserting once, but still fails loudly if it never converges.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last_exc: AssertionError | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            _assert_membership_gone_from_device(vlan_id, ports)
            return
        except AssertionError as exc:
            last_exc = exc
            await asyncio.sleep(1.0)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# HERD API helpers (plain functions, not fixtures: the test builds and tears
# down its own topology/reservation against the pre-seeded lab devices).
# ---------------------------------------------------------------------------


async def _login() -> str:
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/auth/login", json={"email": SEED_EMAIL, "password": SEED_PASSWORD}
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL, verify=False, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    )


async def _device_id_by_name(client, name: str) -> str:
    resp = await client.get("/inventory/devices", params={"name": name, "limit": 50})
    resp.raise_for_status()
    for item in resp.json().get("items", []):
        if item["name"] == name:
            return item["id"]
    raise AssertionError(f"seeded NOS lab device {name!r} not found; run `make seed-nos` first")


def _canvas_edge(dut_a_id: str, dut_b_id: str) -> dict:
    """A direct DUT-to-DUT edge, no switch node: cabling's pathfinder resolves
    it through the seeded physical cabling into two hops on the real switch,
    touching ethernet-1/1 and ethernet-1/2. Mirrors
    tests/integration/test_l2_reconcile.py's canvas shape."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": dut_a_id}}},
            {"id": "nB", "data": {"device": {"id": dut_b_id}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


def _canvas_no_edges(dut_a_id: str, dut_b_id: str) -> dict:
    """The same two device nodes with NO edge between them: the shape the
    reservation activates over, so activation wires nothing and every hop the
    switch ends up carrying is attributable to the later fork save."""
    canvas = _canvas_edge(dut_a_id, dut_b_id)
    canvas["edges"] = []
    return canvas


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"nos-stack-l2-e2e-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_ids: list[str], topology_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": "nos_lab stack e2e: real SR Linux L2 VLAN membership via HERD's API",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _save_fork(client, reservation_id: str, canvas: dict):
    return await client.post(
        f"/reservations/{reservation_id}/fork/save", json={"canvas_data": canvas}
    )


async def _poll_active(client, reservation_id: str, timeout: float = 30.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _wiring_status(client, reservation_id: str) -> dict:
    resp = await client.get(f"/reservations/{reservation_id}/wiring-status")
    resp.raise_for_status()
    return resp.json()


def _l2_rows(status: dict) -> list[dict]:
    return [c for c in status.get("connections", []) if c.get("layer") == "l2"]


async def _poll_wiring_version_applied(
    client, reservation_id: str, version: int, timeout: float = 60.0
) -> dict:
    """Poll until execution has APPLIED at least fork version `version`, and
    return that wiring-status payload.

    `last_applied_fork_version` is execution's own monotonic marker, stamped
    at the END of a wiring_changed pass. Waiting on it is what makes the
    "activation wired nothing" assertion below a real observation rather than
    a race the reconcile simply had not reached yet.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last_status: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        last_status = await _wiring_status(client, reservation_id)
        applied = last_status.get("last_applied_fork_version")
        if applied is not None and applied >= version:
            return last_status
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"execution never applied fork version {version}; last wiring-status: {last_status}"
    )


async def _poll_l2_membership_active(
    client, reservation_id: str, ports: set[str], timeout: float = 60.0
) -> dict[str, dict]:
    """Poll the wiring-status surface until every port in `ports` has an
    ACTIVE l2 row sharing one VLAN id. Returns {port: row}, or raises with the
    last observed status on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_status: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        last_status = await _wiring_status(client, reservation_id)
        by_port = {row["port"]: row for row in _l2_rows(last_status) if row.get("port") in ports}
        if len(by_port) == len(ports) and all(
            row["status"] == "ACTIVE" for row in by_port.values()
        ):
            vlans = {row["vlan"] for row in by_port.values()}
            if len(vlans) == 1 and None not in vlans:
                return by_port
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"L2 membership for {ports} never reached ACTIVE on one VLAN; "
        f"last wiring-status: {last_status}"
    )


async def _poll_l2_membership_released(
    client, reservation_id: str, ports: set[str], timeout: float = 60.0
) -> dict[str, dict]:
    """Poll until every port in `ports` shows a RELEASED l2 row (the as-built
    record after teardown; see the wiring-status endpoint's own docstring)."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_status: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        last_status = await _wiring_status(client, reservation_id)
        by_port = {row["port"]: row for row in _l2_rows(last_status) if row.get("port") in ports}
        if len(by_port) == len(ports) and all(
            row["status"] == "RELEASED" for row in by_port.values()
        ):
            return by_port
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"L2 membership for {ports} never reached RELEASED; last wiring-status: {last_status}"
    )


async def _cleanup(client, *, reservation_id=None, topology_id=None, reservation_cancelled=False):
    """Best-effort teardown in dependency order; never lets one failure hide
    another. The lab devices themselves are seeded groundwork and are never
    deleted here."""
    if reservation_id and not reservation_cancelled:
        await client.delete(f"/reservations/{reservation_id}")
    if topology_id:
        await client.delete(f"/cabling/topologies/{topology_id}")


# ---------------------------------------------------------------------------
# The headline proof: a reservation's wiring derives an L2 VLAN membership
# HERD configures on the real SR Linux switch, and tearing the reservation
# down removes it again, both independently verified against the device, with
# HERD's own wiring-status ledger cross-checked at each step.
# ---------------------------------------------------------------------------


async def test_reservation_derives_and_configures_a_real_l2_vlan_membership_via_stack_api():
    token = await _login()
    async with _client(token) as client:
        dut_1_id = await _device_id_by_name(client, DUT_1_NAME)
        dut_2_id = await _device_id_by_name(client, DUT_2_NAME)
        srl_id = await _device_id_by_name(client, SRL_NAME)

        wired_canvas = _canvas_edge(dut_1_id, dut_2_id)
        ports = {SRL_PORT_1, SRL_PORT_2}

        # Baseline, taken BEFORE the reservation exists: nothing HERD does can
        # race it, and a failure here can only mean the lab really is dirty.
        # (Taken after activation, as this test used to do, the only thing
        # separating the assert from the membership it says is absent is the
        # driver's SSH login, since wiring_changed is already staged.)
        _assert_ports_carry_no_subinterfaces([SRL_PORT_1, SRL_PORT_2])
        baseline_summary = _docker_exec("sr_cli", "show network-instance summary")

        topology_id = None
        reservation_id = None
        reservation_cancelled = False
        vlan_id: int | None = None
        body_failed = False
        try:
            # Activate over an EDGELESS canvas: nothing to wire, so nothing to
            # derive a membership from. The edge arrives in the fork save
            # below, which is what makes the save's effect observable.
            topology_id = await _create_topology(client, _canvas_no_edges(dut_1_id, dut_2_id))
            reservation = await _reserve(client, [dut_1_id, dut_2_id, srl_id], topology_id)
            reservation_id = reservation["id"]
            assert await _poll_active(client, reservation_id), "reservation never activated"

            # Activation stages a reservation.wiring_changed for the fork's
            # first version; wait for execution to have APPLIED it before
            # claiming it wired nothing, otherwise this reads an empty ledger
            # the reconcile simply had not reached yet.
            applied = await _poll_wiring_version_applied(client, reservation_id, 1)
            assert _l2_rows(applied) == [], (
                "an edgeless canvas must derive no L2 membership at activation; "
                f"wiring-status: {applied}"
            )

            # The save: add the DUT-to-DUT edge. cabling's pathfinder resolves
            # it through the seeded physical cabling into two hops on the real
            # switch, reservations stages reservation.wiring_changed, and
            # execution's connection-driven reconcile derives the membership
            # (ADR 0009). Everything asserted after this point is therefore
            # attributable to the SAVE, not to activation.
            saved = await _save_fork(client, reservation_id, wired_canvas)
            assert saved.status_code == 200, saved.text

            # Poll HERD's own wiring-status surface (never the driver's own
            # session) until both switch ports are recorded ACTIVE on one VLAN;
            # read the VLAN id from there instead of assuming a number.
            active_rows = await _poll_l2_membership_active(client, reservation_id, ports)
            vlan_id = active_rows[SRL_PORT_1]["vlan"]
            assert vlan_id is not None
            assert active_rows[SRL_PORT_2]["vlan"] == vlan_id, active_rows

            # Now that the allocated id is known, the baseline can be scoped to
            # the VLAN under test: this exact mac-vrf did not exist before the
            # reservation, so the one on the device now is the one HERD built.
            assert f"vlan{vlan_id}" not in baseline_summary, (
                f"vlan{vlan_id} already existed on the switch before this test "
                f"reserved anything:\n{baseline_summary}"
            )

            # Independent verification on the REAL device: a fresh docker exec
            # session, never the driver's own. Assert the mac-vrf exists AND
            # both subinterfaces are bound into it (the binding, not merely the
            # network-instance's existence).
            _assert_membership_on_device(vlan_id, [SRL_PORT_1, SRL_PORT_2])

            # Teardown: cancelling the reservation freezes and releases the
            # wiring across all layers (mirrors
            # test_frr_l3_via_stack_live.py's route-removal-via-cancel path).
            cancel_resp = await client.delete(f"/reservations/{reservation_id}")
            assert cancel_resp.status_code == 204, cancel_resp.text
            reservation_cancelled = True

            # HERD's own ledger must agree with the device: the l2 rows flip to
            # RELEASED as the release-direction reconcile actually tears down
            # the real membership.
            await _poll_l2_membership_released(client, reservation_id, ports)

            # Independent verification: the real device no longer carries the
            # mac-vrf or either subinterface binding. delete_vlan runs in its
            # own driver session AFTER the ledger already reports RELEASED
            # (see the poll helper's docstring), so this polls rather than
            # asserting once.
            await _poll_membership_gone_from_device(vlan_id, [SRL_PORT_1, SRL_PORT_2])
        except BaseException:
            # Remembered so the cleanup check below cannot REPLACE a real
            # failure with a cleanup complaint; it only speaks up when the
            # body itself passed.
            body_failed = True
            raise
        finally:
            # Defense in depth: if an assertion failed before teardown ran (or
            # teardown itself only partially converged), remove any leftover
            # membership directly so the lab stays re-runnable without a reset.
            cleanup_error = (
                _remove_membership_from_device(vlan_id, [SRL_PORT_1, SRL_PORT_2])
                if vlan_id is not None
                else None
            )
            await _cleanup(
                client,
                reservation_id=reservation_id,
                topology_id=topology_id,
                reservation_cancelled=reservation_cancelled,
            )
            if cleanup_error is not None and not body_failed:
                raise AssertionError(cleanup_error)
