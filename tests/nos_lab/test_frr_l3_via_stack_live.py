"""End-to-end proof: HERD's own API drives a REAL FRR router through its
execution service and driver sandbox (drivers/frr_l3), against the checked-in
NOS test lab (infra/nos-test, docs/NOS_LAB.md). This automates the equivalent
of docs/MANUAL_TESTING.md case M1 for the Layer 3 Switch driver contract.

Why this is NOT the M1 config-apply flow verbatim: frr_l3 implements the
narrower Layer 3 Switch contract (login, logout, configure_route,
remove_route, status), not the Management contract's generic configure()
job M1 was originally written against (see docs/DRIVERS.md and
seedtools.nos_lab's seed_nos_lab docstring). HERD only ever drives a
Layer 3 Switch through a reservation fork's routing intent (ADR 0009/0014),
so "through HERD's normal path" here means: create a device, a config
version, a topology whose switch node carries data.l3.routes, and a
reservation over it, then let the reservation's activation and fork-save
reconcile drive the real device. This is the exact shape
tests/integration/test_l3_intent_execution.py already proves against the
mock_l3 driver; this file is that same shape against the REAL frr_l3 driver
and a REAL device, plus independent on-device verification.

Exactly what the two tests here prove, in order:

  (a) ACTIVATION applies the fork's routing intent: the route the canvas
      carried at reserve time is installed on the real router (create_fork
      writes intent tolerantly and does not gate, ADR 0014).
  (b) A fork SAVE that CHANGES the route set runs the gated save path
      (gate_l3_intent) and drives the resulting route-set delta: the save
      stages reservation.wiring_changed, execution's stay-adjacent reconcile
      computes removes = pinned - intent and adds = intent - pinned, and
      drives removes BEFORE adds inside one login/logout (ADR 0014
      Decision 3). Both halves are verified on the real router: the old
      prefix is gone and the new one is installed.
  (c) CANCELLING the reservation deprovisions exactly the applied set.
  (d) A route the real device REJECTS records a FAILED execution run
      carrying the device's own vtysh wording, and installs nothing.

Every destination assertion matches the FULL prefix FRR prints (192.0.2.4/30),
never the bare network address: the bare form would also match a leaked
neighbouring prefix such as 192.0.2.40/30.

Placement and gating: this is the one test in the repo that needs BOTH the
NOS test lab (make nos-up) AND a running dev stack (make up) with the lab
attached to the stack's Docker network (make nos-attach), so the execution
service can reach nos-test-frr by container name over Docker DNS. It lives
under tests/nos_lab/ (never invoked by make test, make master, or make
everything) rather than tests/integration/ (which IS invoked by master/
everything, against an ephemeral gate stack the lab is never attached to);
see the phase 3a report for the full placement rationale. Both preconditions
are checked independently; set HERD_TEST_NOS_REQUIRED=1 to turn a missing
precondition into a hard failure instead of a skip, mirroring
test_frr_l3_driver_live.py's own gating.

Every change is verified independently of the driver's own session and the
execution run's own success flag: a separate `docker exec nos-test-frr
vtysh -c ...` call, never trusted implicitly, exactly like
test_frr_l3_driver_live.py.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import random
import re
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

# Reuse seedtools' driver-zip-from-disk helper and constants instead of a
# third copy of "zip a real driver package from disk" logic.
from seedtools.catalog import SECTIONS
from seedtools.drivers import _make_driver_zip_from_dir, _make_dummy_zip
from seedtools.nos_lab import FRR_L3_DRIVER_DIR

_REPO_ROOT = Path(__file__).resolve().parents[2]

FRR_HOST = os.getenv("HERD_TEST_FRR_HOST", "127.0.0.1")
FRR_PORT = int(os.getenv("HERD_TEST_FRR_PORT", "2224"))
FRR_CONTAINER = "nos-test-frr"
FRR_LOGIN = "netadmin"
FRR_PASSWORD = "netadmin"

BASE_URL = os.getenv("HERD_BASE_URL", "https://localhost/api")
SEED_EMAIL = os.getenv("SEED_EMAIL") or os.getenv("SUPERADMIN_EMAIL", "admin@example.com")
SEED_PASSWORD = os.getenv("SEED_PASSWORD") or os.getenv("SUPERADMIN_PASSWORD", "admin123!")


# ---------------------------------------------------------------------------
# Preconditions: the lab reachable, the lab ATTACHED to the stack's network,
# and the stack itself reachable. Skipped by default; HERD_TEST_NOS_REQUIRED=1
# turns any missing precondition into a hard failure.
# ---------------------------------------------------------------------------


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _lab_attached_to_stack() -> bool:
    """True iff nos-test-frr can resolve a HERD service by Docker DNS, proving
    it is attached to the stack's Docker network (`make nos-attach`).
    Direction-agnostic by design: any container on the same user-defined
    bridge network can resolve any other container's name/aliases, so this
    does not need to know the stack's compose project name."""
    try:
        result = subprocess.run(
            ["docker", "exec", FRR_CONTAINER, "getent", "hosts", "execution"],
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

    Resolved from the ENVIRONMENT, not from .env: this mirrors tests/e2e/conftest.py,
    but the failure mode there is a bare 401 deep inside a test. The stack seeds its
    superadmin from .env, so a shell that has not exported those values falls back to
    the generic default and gets 401. Probing here turns that into a precondition
    message naming the fix instead.
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
    if not _reachable(FRR_HOST, FRR_PORT):
        return (
            f"NOS test lab FRR node not reachable ({FRR_HOST}:{FRR_PORT}); "
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


def _docker_exec(container: str, *args: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, *args], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"docker exec {container} {' '.join(args)} failed (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout


def _show_ip_route_static() -> str:
    return _docker_exec(FRR_CONTAINER, "vtysh", "-c", "show ip route static")


# The VRF fixture infra/nos-test/frr/start.sh creates at boot (ADR 0014 addendum
# X-G, issue #755; docs/NOS_LAB.md): `blue` maps to routing table 10 and owns
# `dummy0` at 192.0.2.254/30. Permanent by design, so this file cleans up only
# its own routes.
VRF_NAME = "blue"
VRF_TABLE = "10"
VRF_INTERFACE = "dummy0"
VRF_INTERFACE_CIDR = "192.0.2.254/30"
VRF_MEMBER_SUBNET = "192.0.2.252/30"
VRF_NEXT_HOP = "192.0.2.253"
UNKNOWN_VRF_NAME = "no-such-vrf"


def _vrf_kernel_routes() -> str:
    """The kernel routing table the VRF maps to, read WITHOUT going through FRR
    at all: the most independent verification channel available here."""
    return _docker_exec(FRR_CONTAINER, "ip", "route", "show", "table", VRF_TABLE)


def _vrf_frr_routes() -> str:
    return _docker_exec(FRR_CONTAINER, "vtysh", "-c", f"show ip route vrf {VRF_NAME} static")


def _remove_vrf_route_on_device(destination: str, next_hop: str | None) -> None:
    """The VRF counterpart of _remove_route_on_device; same best-effort role."""
    target = next_hop if next_hop else VRF_INTERFACE
    subprocess.run(
        [
            "docker",
            "exec",
            FRR_CONTAINER,
            "vtysh",
            "-c",
            "configure terminal",
            "-c",
            f"no ip route {destination} {target} vrf {VRF_NAME}",
            "-c",
            "end",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


def _frr_eth0_cidr() -> str:
    addr_output = _docker_exec(FRR_CONTAINER, "ip", "-4", "-o", "addr", "show", "eth0")
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr_output)
    assert match, f"could not parse eth0 address from: {addr_output!r}"
    return match.group(1)


def _connected_nexthop(cidr: str) -> str:
    """An address on `cidr`'s own network, distinct from the interface's own
    address, so a next-hop route toward it resolves and is SELECTED (visible
    under `show ip route static`) instead of sitting unresolved."""
    iface = ipaddress.ip_interface(cidr)
    candidate = iface.network.broadcast_address - 1
    if candidate == iface.ip:
        candidate = iface.network.broadcast_address - 2
    return str(candidate)


def _unique_test_prefix(*, exclude: tuple[str, ...] = ()) -> str:
    """A random, per-run-unique /30 destination inside RFC5737 TEST-NET-1,
    never one of `exclude` (the route-set delta phase needs two distinct
    prefixes, and a 64-subnet pool collides often enough to matter), and never
    the subnet the lab's VRF member interface occupies."""
    network = ipaddress.ip_network("192.0.2.0/24")
    subnets = [
        str(s)
        for s in network.subnets(new_prefix=30)
        if str(s) not in exclude and str(s) != VRF_MEMBER_SUBNET
    ]
    return random.choice(subnets)


def _assert_route_installed(routes_output: str, destination: str) -> None:
    """`show ip route static` lists the FULL prefix, so assert the full prefix.

    Matching only the network address (destination.split("/")[0]) would pass
    against a leaked NEIGHBOUR prefix: "192.0.2.4" is a substring of
    "192.0.2.40/30". tests/nos_lab/test_nos_lab_live.py asserts the full
    prefix for exactly this reason; this is the same rule applied to the
    via-stack path.
    """
    assert destination in routes_output, (
        f"expected the full prefix {destination} in `show ip route static`:\n{routes_output}"
    )


def _assert_route_absent(routes_output: str, destination: str) -> None:
    """The mirror of _assert_route_installed: the FULL prefix is gone."""
    assert destination not in routes_output, (
        f"expected the full prefix {destination} to be absent from "
        f"`show ip route static`:\n{routes_output}"
    )


def _remove_route_on_device(destination: str, next_hop: str | None) -> None:
    """Defence in depth: drop `destination` straight off the router if the API
    path left it behind (an assertion failed before the teardown step ran).
    Best-effort by design; the test's own assertions are what prove the
    product path works."""
    command = f"no ip route {destination}"
    if next_hop:
        command = f"{command} {next_hop}"
    subprocess.run(
        [
            "docker",
            "exec",
            FRR_CONTAINER,
            "vtysh",
            "-c",
            "configure terminal",
            "-c",
            command,
            "-c",
            "end",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# HERD API helpers (plain functions, not fixtures: each test builds and tears
# down its own fully isolated driver/template/device/topology/reservation, so
# nothing here needs cross-test scope).
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


async def _upload_driver(client, name: str, connection_type: str, zip_bytes: bytes) -> dict:
    files = {"file": (f"{name}.zip", zip_bytes, "application/zip")}
    data = {
        "name": name,
        "connection_type": connection_type,
        "description": "nos_lab stack e2e test",
    }
    resp = await client.post("/inventory/drivers", files=files, data=data)
    resp.raise_for_status()
    return resp.json()


async def _create_template(client, driver_id: str, name: str) -> dict:
    resp = await client.post(
        "/inventory/templates",
        json={
            "name": name,
            "template_type": "device",
            "driver_id": driver_id,
            "vendor": "IntegrationVendor",
            "model": "nos_lab stack e2e",
            "sections": SECTIONS,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _create_device(client, template_id: str, name: str, field_data: dict) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "field_data": field_data,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _set_config(
    client, device_id: str, interfaces: list[dict], virtual_routers: list[dict] | None = None
) -> dict:
    config: dict = {"interfaces": interfaces}
    if virtual_routers is not None:
        # ADR 0014 addendum X-I (issue #755): the declared virtual routers a
        # route's `virtual_router` is validated against, at the save gate and at
        # drive-time re-validation alike.
        config["virtual_routers"] = virtual_routers
    resp = await client.post(
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": config, "description": "nos_lab stack e2e"},
    )
    assert resp.status_code == 201, f"config-version create failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _create_connection(client, dut_id: str, switch_id: str, switch_port: str) -> dict:
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": dut_id,
            "port_a": "eth0",
            "device_b_id": switch_id,
            "port_b": switch_port,
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _canvas_with_l3(dut_id: str, switch_id: str, routes: list[dict] | None) -> dict:
    switch_data: dict = {"device": {"id": switch_id}}
    if routes is not None:
        switch_data["l3"] = {"routes": routes}
    return {
        "nodes": [
            {"id": "nDut", "data": {"device": {"id": dut_id}}},
            {"id": "nSwitch", "data": switch_data},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nDut",
                "target": "nSwitch",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"nos-stack-e2e-{uuid.uuid4().hex[:8]}"}
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
            "purpose": "nos_lab stack e2e: real FRR route through HERD's own API",
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


async def _runs(client, reservation_id: str, action: str, status: str = "SUCCESS") -> list[dict]:
    resp = await client.get(
        "/execution/runs", params={"reservation_id": reservation_id, "status": status, "limit": 300}
    )
    # Never swallow a non-200 into an empty list: that turns an API error into
    # the far more misleading "the route was never configured".
    assert resp.status_code == 200, f"GET /execution/runs failed: {resp.status_code} {resp.text}"
    return [r for r in resp.json().get("items", []) if r["action"] == action]


async def _poll_route_run(
    client,
    reservation_id: str,
    action: str,
    destination: str,
    *,
    status: str = "SUCCESS",
    timeout: float = 45.0,
) -> dict | None:
    """Poll for a run of `action`/`status` whose route destination (packed into
    port_a by execution's _route_run_identity) matches; returns it or None."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for r in await _runs(client, reservation_id, action, status=status):
            if r.get("port_a") == destination:
                return r
        await asyncio.sleep(0.5)
    return None


async def _cleanup(
    client,
    *,
    reservation_id=None,
    topology_id=None,
    connection_id=None,
    device_ids=(),
    template_ids=(),
    driver_ids=(),
):
    """Best-effort teardown in dependency order; never lets one failure hide another."""
    if reservation_id:
        await client.delete(f"/reservations/{reservation_id}")
    if topology_id:
        await client.delete(f"/cabling/topologies/{topology_id}")
    if connection_id:
        await client.delete(f"/cabling/connections/{connection_id}")
    for device_id in device_ids:
        await client.delete(f"/inventory/devices/{device_id}")
    for template_id in template_ids:
        await client.delete(f"/inventory/templates/{template_id}")
    for driver_id in driver_ids:
        await client.delete(f"/inventory/drivers/{driver_id}")


# ---------------------------------------------------------------------------
# (a) The headline proof, in three phases against the one real router:
#   1. ACTIVATION applies the fork's routing intent (route A installed).
#   2. A fork SAVE carrying a CHANGED route set (route B instead of route A)
#      runs the gated save path and drives the route-set delta: removes
#      before adds, inside one login/logout (ADR 0014 Decision 3). A is gone
#      from the router, B is installed.
#   3. CANCELLING deprovisions the applied set (route B removed).
# Every device read is an independent `docker exec ... vtysh` call.
# ---------------------------------------------------------------------------


async def test_reservation_applies_changes_and_removes_real_static_routes_via_stack_api():
    suffix = uuid.uuid4().hex[:8]
    token = await _login()
    async with _client(token) as client:
        driver = await _upload_driver(
            client,
            f"nos-stack-frr-l3-{suffix}",
            "Layer 3 Switch",
            _make_driver_zip_from_dir(FRR_L3_DRIVER_DIR),
        )
        switch_template = await _create_template(
            client, driver["id"], f"nos-stack-frr-l3-tmpl-{suffix}"
        )
        dut_driver = await _upload_driver(
            client,
            f"nos-stack-dut-driver-{suffix}",
            "Management",
            _make_dummy_zip("nos-stack-dut"),
        )
        dut_template = await _create_template(
            client, dut_driver["id"], f"nos-stack-dut-tmpl-{suffix}"
        )

        switch = await _create_device(
            client,
            switch_template["id"],
            f"nos-stack-frr-{suffix}",
            {"ip": FRR_CONTAINER, "login": FRR_LOGIN, "password": FRR_PASSWORD},
        )
        dut = await _create_device(
            client,
            dut_template["id"],
            f"nos-stack-dut-{suffix}",
            {"ip": "192.0.2.240", "login": "x", "password": "x"},
        )

        eth0_cidr = _frr_eth0_cidr()
        await _set_config(
            client, switch["id"], [{"name": "eth0", "ip": eth0_cidr, "zone": "trust"}]
        )

        next_hop = _connected_nexthop(eth0_cidr)
        # Two distinct destinations: the first lands at activation, the second
        # replaces it through a fork save so the save's route-set delta has
        # both a remove and an add to drive.
        destination = _unique_test_prefix()
        destination_after_save = _unique_test_prefix(exclude=(destination,))
        route = {"destination": destination, "next_hop": next_hop, "interface": "eth0"}
        route_after_save = {
            "destination": destination_after_save,
            "next_hop": next_hop,
            "interface": "eth0",
        }

        connection = None
        topology_id = None
        reservation_id = None
        reservation_cancelled = False
        try:
            connection = await _create_connection(client, dut["id"], switch["id"], "ge-0/0/1")
            topology_id = await _create_topology(
                client, _canvas_with_l3(dut["id"], switch["id"], [route])
            )
            reservation = await _reserve(client, [dut["id"], switch["id"]], topology_id)
            reservation_id = reservation["id"]
            assert await _poll_active(client, reservation_id), "reservation never activated"

            # Phase 1: activation applied the canvas intent. _poll_route_run
            # already filters on status=SUCCESS, so finding the run IS the
            # status assertion; re-asserting it would be a tautology.
            run = await _poll_route_run(client, reservation_id, "configure_route", destination)
            assert run is not None, (
                "the intent route was never configured (no SUCCESS configure_route run)"
            )

            # Independent verification: a fresh vtysh call, never the driver's
            # own session or the execution run's own success flag.
            _assert_route_installed(_show_ip_route_static(), destination)

            fork = (await client.get(f"/reservations/{reservation_id}/fork")).json()
            assert len(fork["l3_routes"]) == 1
            assert fork["l3_routes"][0]["destination"] == destination

            # Phase 2: a fork SAVE with a CHANGED route set. Activation
            # (create_fork) writes intent tolerantly and deliberately does not
            # gate, so phase 1 alone never exercises the save path. This save
            # does: gate_l3_intent runs, the version advances, reservations
            # stages reservation.wiring_changed, and execution's stay-adjacent
            # reconcile computes removes = pinned - intent and adds = intent -
            # pinned by route identity, driving removes BEFORE adds within one
            # login/logout (ADR 0014 Decision 3). Both halves are then checked
            # on the router itself, which is the only place a "removes before
            # adds, in one session" claim can actually be falsified.
            saved = await _save_fork(
                client,
                reservation_id,
                _canvas_with_l3(dut["id"], switch["id"], [route_after_save]),
            )
            assert saved.status_code == 200, saved.text

            delta_add = await _poll_route_run(
                client, reservation_id, "configure_route", destination_after_save
            )
            assert delta_add is not None, (
                "the fork save's added route was never configured (no SUCCESS "
                f"configure_route run for {destination_after_save})"
            )
            delta_remove = await _poll_route_run(
                client, reservation_id, "remove_route", destination
            )
            assert delta_remove is not None, (
                "the fork save's departed route was never removed (no SUCCESS "
                f"remove_route run for {destination})"
            )

            # The router itself reflects the delta: the old prefix is gone and
            # the new one is installed. Read once, after both runs landed: on
            # the BUILD direction the device leads the ledger, so a single read
            # after the run appears is correctly ordered.
            routes_after_save = _show_ip_route_static()
            _assert_route_absent(routes_after_save, destination)
            _assert_route_installed(routes_after_save, destination_after_save)

            # HERD's own fork surface agrees: the saved intent IS the new set.
            fork_after_save = (await client.get(f"/reservations/{reservation_id}/fork")).json()
            assert [r["destination"] for r in fork_after_save["l3_routes"]] == [
                destination_after_save
            ], fork_after_save["l3_routes"]

            # Phase 3: remove the route. Cancelling the reservation deprovisions exactly
            # the applied (intent-derived) set (ADR 0014; matches
            # test_l3_intent_execution.py's mock-driver equivalent). A fork
            # save that merely CLEARS intent while the switch stays wired
            # deliberately does NOT do this (addendum X4, "no surprise
            # teardown": proven live while building this test), which is why
            # phase 2 above CHANGED the route set rather than emptying it; so
            # cancel is the correct "remove it" path here, not an empty save.
            cancel_resp = await client.delete(f"/reservations/{reservation_id}")
            assert cancel_resp.status_code == 204, cancel_resp.text
            reservation_cancelled = True

            remove_run = await _poll_route_run(
                client, reservation_id, "remove_route", destination_after_save
            )
            assert remove_run is not None, (
                "the route was never removed (no SUCCESS remove_route run for "
                f"{destination_after_save})"
            )

            _assert_route_absent(_show_ip_route_static(), destination_after_save)
        finally:
            # Defense in depth: remove BOTH prefixes directly if the API path
            # left either behind (e.g. an earlier assertion failed before the
            # cancel ran).
            _remove_route_on_device(destination, next_hop)
            _remove_route_on_device(destination_after_save, next_hop)
            await _cleanup(
                client,
                reservation_id=None if reservation_cancelled else reservation_id,
                topology_id=topology_id,
                connection_id=connection["id"] if connection else None,
                device_ids=(switch["id"], dut["id"]),
                template_ids=(switch_template["id"], dut_template["id"]),
                driver_ids=(driver["id"], dut_driver["id"]),
            )


# ---------------------------------------------------------------------------
# (b) The device REJECTS the command HERD sent: the execution run must
# record FAILED, and independent verification must show nothing installed.
# This is the live, full-stack proof of the #771 driver-result-payload
# contract for drivers/frr_l3 (already implemented; #771 itself tracks the
# OLDER frr_mgmt driver, which has not yet been fixed).
#
# A syntactically malformed destination/next-hop (e.g. bad octets) CANNOT
# reach the device this way: cabling's own save-time gate validates every
# route with ipaddress.ip_network()/ip_address() before ever accepting the
# fork save (services/cabling/app/services/l3_validation.py), so a bad
# octet is refused at 422 l3_intent_malformed and never reaches execution
# or the driver at all (verified live against this exact lab while building
# this test: `ip route 999.999.999.0/24 172.17.0.1` reproduces the classic
# "% Unknown command" rejection directly over vtysh, but the intent path
# never lets that string past the fork save).
#
# Instead this drives a route HERD accepts as syntactically valid (a plain
# string interface name, `_require_string` imposes no character
# restriction) but the REAL device rejects, because the interface name
# embeds a second token vtysh cannot parse. Verified live against this
# exact lab: `ip route <dest> "eth0 extra-token"` becomes
# `ip route <dest> eth0 extra-token` over vtysh, which answers
# "% Unknown command: ...". This is an interface-route (next_hop=None) so
# execution's own subnet-membership check for next_hop never applies.
# ---------------------------------------------------------------------------


async def test_rejected_route_records_a_failed_execution_run_via_stack_api():
    suffix = uuid.uuid4().hex[:8]
    bogus_interface = "eth0 rejected-by-device"
    token = await _login()
    async with _client(token) as client:
        driver = await _upload_driver(
            client,
            f"nos-stack-frr-l3-rej-{suffix}",
            "Layer 3 Switch",
            _make_driver_zip_from_dir(FRR_L3_DRIVER_DIR),
        )
        switch_template = await _create_template(
            client, driver["id"], f"nos-stack-frr-l3-rej-tmpl-{suffix}"
        )
        dut_driver = await _upload_driver(
            client,
            f"nos-stack-dut-driver-rej-{suffix}",
            "Management",
            _make_dummy_zip("nos-stack-dut-rej"),
        )
        dut_template = await _create_template(
            client, dut_driver["id"], f"nos-stack-dut-rej-tmpl-{suffix}"
        )

        switch = await _create_device(
            client,
            switch_template["id"],
            f"nos-stack-frr-rej-{suffix}",
            {"ip": FRR_CONTAINER, "login": FRR_LOGIN, "password": FRR_PASSWORD},
        )
        dut = await _create_device(
            client,
            dut_template["id"],
            f"nos-stack-dut-rej-{suffix}",
            {"ip": "192.0.2.241", "login": "x", "password": "x"},
        )

        eth0_cidr = _frr_eth0_cidr()
        await _set_config(
            client, switch["id"], [{"name": bogus_interface, "ip": eth0_cidr, "zone": "trust"}]
        )

        destination = _unique_test_prefix()
        route = {"destination": destination, "next_hop": None, "interface": bogus_interface}

        connection = None
        topology_id = None
        reservation_id = None
        try:
            connection = await _create_connection(client, dut["id"], switch["id"], "ge-0/0/1")
            topology_id = await _create_topology(
                client, _canvas_with_l3(dut["id"], switch["id"], [route])
            )
            reservation = await _reserve(client, [dut["id"], switch["id"]], topology_id)
            reservation_id = reservation["id"]
            assert await _poll_active(client, reservation_id), "reservation never activated"

            failed_run = await _poll_route_run(
                client, reservation_id, "configure_route", destination, status="FAILED"
            )
            assert failed_run is not None, (
                "expected a FAILED configure_route execution run for a route the "
                "device rejects; HERD's ledger must not record success for a "
                "config the router refused"
            )
            # The device's OWN wording, not merely "some non-empty string":
            # vtysh answers a command it cannot parse with a "%"-prefixed line
            # that echoes the offending command back, and drivers/frr_l3
            # reports that line verbatim as the run's error (issue #779). The
            # full destination and the bogus interface must both appear, so a
            # driver that invented a generic message, or reported the wrong
            # route's rejection, fails here.
            error_text = failed_run["error"] or ""
            assert error_text.startswith("% Unknown command:"), error_text
            assert destination in error_text, error_text
            assert bogus_interface in error_text, error_text

            # No SUCCESS run for this destination: the driver must never have
            # reported success for a command the device rejected.
            success_runs = await _runs(client, reservation_id, "configure_route", status="SUCCESS")
            assert destination not in {r.get("port_a") for r in success_runs}

            # Independent verification: the real device never installed it.
            _assert_route_absent(_show_ip_route_static(), destination)
        finally:
            await _cleanup(
                client,
                reservation_id=reservation_id,
                topology_id=topology_id,
                connection_id=connection["id"] if connection else None,
                device_ids=(switch["id"], dut["id"]),
                template_ids=(switch_template["id"], dut_template["id"]),
                driver_ids=(driver["id"], dut_driver["id"]),
            )


# ---------------------------------------------------------------------------
# (c) VRF, end to end through HERD's own API (ADR 0014 addenda X-G and X-I,
# issue #755), in two phases against the one real router:
#   1. A reservation whose switch config DECLARES virtual router `blue` and
#      whose canvas carries a route into it installs that route in the real
#      VRF, proven through the kernel's own table 10 and through FRR's per-VRF
#      view, and proven ABSENT from the default table (a VRF route that leaked
#      into the default table is the exact silent failure X-G exists to
#      prevent).
#   2. A fork save naming a virtual router the config does NOT declare is
#      refused by the save gate with l3_unknown_virtual_router, and the router
#      is untouched.
# The driver half (rendering, the not-installed classification) is proven in
# tests/nos_lab/test_frr_l3_driver_live.py; this file proves the PATH: the
# capability claim reached execution, the keyword reached the driver, and the
# config-declared virtual routers gated the save.
# ---------------------------------------------------------------------------


async def test_vrf_routing_intent_installs_in_the_real_vrf_via_stack_api():
    suffix = uuid.uuid4().hex[:8]
    token = await _login()
    async with _client(token) as client:
        driver = await _upload_driver(
            client,
            f"nos-stack-frr-l3-vrf-{suffix}",
            "Layer 3 Switch",
            _make_driver_zip_from_dir(FRR_L3_DRIVER_DIR),
        )
        # The capability claim is what makes execution pass `virtual_router` at
        # all; asserting it here turns a silently-non-declaring upload into a
        # clear failure rather than an unexplained l3_vrf_unsupported below.
        assert driver["supports_vrf"] is True, driver

        switch_template = await _create_template(
            client, driver["id"], f"nos-stack-frr-l3-vrf-tmpl-{suffix}"
        )
        dut_driver = await _upload_driver(
            client,
            f"nos-stack-dut-driver-vrf-{suffix}",
            "Management",
            _make_dummy_zip("nos-stack-dut-vrf"),
        )
        dut_template = await _create_template(
            client, dut_driver["id"], f"nos-stack-dut-vrf-tmpl-{suffix}"
        )

        switch = await _create_device(
            client,
            switch_template["id"],
            f"nos-stack-frr-vrf-{suffix}",
            {"ip": FRR_CONTAINER, "login": FRR_LOGIN, "password": FRR_PASSWORD},
        )
        dut = await _create_device(
            client,
            dut_template["id"],
            f"nos-stack-dut-vrf-{suffix}",
            {"ip": "192.0.2.242", "login": "x", "password": "x"},
        )

        eth0_cidr = _frr_eth0_cidr()
        await _set_config(
            client,
            switch["id"],
            [
                {"name": "eth0", "ip": eth0_cidr, "zone": "trust"},
                {"name": VRF_INTERFACE, "ip": VRF_INTERFACE_CIDR, "zone": "trust"},
            ],
            virtual_routers=[{"name": VRF_NAME, "interfaces": [VRF_INTERFACE]}],
        )

        destination = _unique_test_prefix()
        vrf_route = {
            "destination": destination,
            "next_hop": VRF_NEXT_HOP,
            "interface": VRF_INTERFACE,
            "virtual_router": VRF_NAME,
        }

        connection = None
        topology_id = None
        reservation_id = None
        reservation_cancelled = False
        try:
            connection = await _create_connection(client, dut["id"], switch["id"], "ge-0/0/1")
            topology_id = await _create_topology(
                client, _canvas_with_l3(dut["id"], switch["id"], [vrf_route])
            )
            reservation = await _reserve(client, [dut["id"], switch["id"]], topology_id)
            reservation_id = reservation["id"]
            assert await _poll_active(client, reservation_id), "reservation never activated"

            run = await _poll_route_run(client, reservation_id, "configure_route", destination)
            assert run is not None, (
                "the VRF intent route was never configured (no SUCCESS configure_route "
                "run); an l3_vrf_unsupported park means the capability claim did not "
                "reach execution"
            )
            # The run identity packs the VRF, so two routes differing only by
            # virtual router stay distinct guarded actions.
            assert run["port_b"].endswith(f"|{VRF_NAME}"), run["port_b"]

            # Independent verification, two channels, neither the driver's own
            # session: the kernel table the VRF maps to, and FRR's per-VRF view.
            assert destination in _vrf_kernel_routes(), _vrf_kernel_routes()
            assert destination in _vrf_frr_routes(), _vrf_frr_routes()
            # And NOT in the default table.
            _assert_route_absent(_show_ip_route_static(), destination)

            # Phase 2: a save naming an undeclared virtual router is refused by
            # the gate; nothing reaches the device.
            bad_route = dict(vrf_route, virtual_router=UNKNOWN_VRF_NAME)
            refused = await _save_fork(
                client, reservation_id, _canvas_with_l3(dut["id"], switch["id"], [bad_route])
            )
            assert refused.status_code == 409, refused.text
            body = refused.json()
            reasons = {
                entry["reason"] for entry in (body.get("detail") or {}).get("invalid_routes", [])
            }
            assert "l3_unknown_virtual_router" in reasons, body
            # The applied route is untouched: a refused save drives nothing.
            assert destination in _vrf_kernel_routes()

            # Cancelling deprovisions exactly the applied set, out of the VRF.
            cancel_resp = await client.delete(f"/reservations/{reservation_id}")
            assert cancel_resp.status_code == 204, cancel_resp.text
            reservation_cancelled = True
            remove_run = await _poll_route_run(client, reservation_id, "remove_route", destination)
            assert remove_run is not None, (
                f"the VRF route was never removed (no SUCCESS remove_route run for {destination})"
            )
            assert destination not in _vrf_kernel_routes(), _vrf_kernel_routes()
        finally:
            _remove_vrf_route_on_device(destination, VRF_NEXT_HOP)
            await _cleanup(
                client,
                reservation_id=None if reservation_cancelled else reservation_id,
                topology_id=topology_id,
                connection_id=connection["id"] if connection else None,
                device_ids=(switch["id"], dut["id"]),
                template_ids=(switch_template["id"], dut_template["id"]),
                driver_ids=(driver["id"], dut_driver["id"]),
            )
