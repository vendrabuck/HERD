"""End-to-end proof: HERD's own API drives a REAL FRR router through its
execution service and driver sandbox (drivers/frr_l3), against the checked-in
NOS test lab (infra/nos-test, docs/NOS_LAB.md). This automates the equivalent
of docs/MANUAL_TESTING.md case M1 for the Layer 3 Switch driver contract.

Why this is NOT the M1 config-apply flow verbatim: frr_l3 implements the
narrower Layer 3 Switch contract (login, logout, configure_route,
remove_route, status), not the Management contract's generic configure()
job M1 was originally written against (see docs/DRIVERS.md and
seed_devices_public.py's seed_nos_lab docstring). HERD only ever drives a
Layer 3 Switch through a reservation fork's routing intent (ADR 0009/0014),
so "through HERD's normal path" here means: create a device, a config
version, a topology whose switch node carries data.l3.routes, and a
reservation over it, then let the reservation's activation and fork-save
reconcile drive the real device. This is the exact shape
tests/integration/test_l3_intent_execution.py already proves against the
mock_l3 driver; this file is that same shape against the REAL frr_l3 driver
and a REAL device, plus independent on-device verification.

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
import importlib.util
import ipaddress
import os
import random
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Reuse seed_devices_public.py's driver-zip-from-disk helper and constants
# instead of a third copy of "zip a real driver package from disk" logic.
_seed_spec = importlib.util.spec_from_file_location(
    "seed_devices_public_for_nos_stack_test", _REPO_ROOT / "seed_devices_public.py"
)
assert _seed_spec is not None and _seed_spec.loader is not None
_seed = importlib.util.module_from_spec(_seed_spec)
sys.modules[_seed_spec.name] = _seed
_seed_spec.loader.exec_module(_seed)

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


_FRR_REACHABLE = _reachable(FRR_HOST, FRR_PORT)
_LAB_ATTACHED = _FRR_REACHABLE and _lab_attached_to_stack()
_STACK_REACHABLE = _stack_reachable()
_PRECONDITIONS_MET = _FRR_REACHABLE and _LAB_ATTACHED and _STACK_REACHABLE
_NOS_REQUIRED = os.getenv("HERD_TEST_NOS_REQUIRED", "") not in ("", "0")


def _missing_precondition_reason() -> str:
    if not _FRR_REACHABLE:
        return (
            f"NOS test lab FRR node not reachable ({FRR_HOST}:{FRR_PORT}); "
            "start it with `make nos-up`."
        )
    if not _LAB_ATTACHED:
        return (
            "NOS test lab is not attached to the dev stack's Docker network; "
            "run `make nos-attach` (dev stack must be up: `make up`)."
        )
    if not _STACK_REACHABLE:
        return f"HERD stack not reachable at {BASE_URL}; run `make up`."
    return ""


pytestmark = pytest.mark.skipif(
    not _NOS_REQUIRED and not _PRECONDITIONS_MET,
    reason=_missing_precondition_reason() or "NOS lab + stack preconditions not met",
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unavailable():
    if _NOS_REQUIRED and not _PRECONDITIONS_MET:
        pytest.fail(_missing_precondition_reason())


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


def _unique_test_prefix() -> str:
    """A random, per-run-unique /30 destination inside RFC5737 TEST-NET-1."""
    network = ipaddress.ip_network("192.0.2.0/24")
    subnets = list(network.subnets(new_prefix=30))
    return str(random.choice(subnets))


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
            "sections": _seed.SECTIONS,
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


async def _set_config(client, device_id: str, interfaces: list[dict]) -> dict:
    resp = await client.post(
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": {"interfaces": interfaces}, "description": "nos_lab stack e2e"},
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
    if resp.status_code != 200:
        return []
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
# (a) The headline proof: HERD's API drives a real static route onto the
# real FRR router, and removes it again, both independently verified.
# ---------------------------------------------------------------------------


async def test_reservation_applies_and_removes_a_real_static_route_via_stack_api():
    suffix = uuid.uuid4().hex[:8]
    token = await _login()
    async with _client(token) as client:
        driver = await _upload_driver(
            client,
            f"nos-stack-frr-l3-{suffix}",
            "Layer 3 Switch",
            _seed._make_driver_zip_from_dir(_seed.FRR_L3_DRIVER_DIR),
        )
        switch_template = await _create_template(
            client, driver["id"], f"nos-stack-frr-l3-tmpl-{suffix}"
        )
        dut_driver = await _upload_driver(
            client,
            f"nos-stack-dut-driver-{suffix}",
            "Management",
            _seed._make_dummy_zip("nos-stack-dut"),
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

        destination = _unique_test_prefix()
        next_hop = _connected_nexthop(eth0_cidr)
        route = {"destination": destination, "next_hop": next_hop, "interface": "eth0"}

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

            run = await _poll_route_run(client, reservation_id, "configure_route", destination)
            assert run is not None, (
                "the intent route was never configured (no SUCCESS configure_route run)"
            )
            assert run["status"] == "SUCCESS"

            # Independent verification: a fresh vtysh call, never the driver's
            # own session or the execution run's own success flag.
            routes_after_apply = _show_ip_route_static()
            assert destination.split("/")[0] in routes_after_apply, routes_after_apply

            fork = (await client.get(f"/reservations/{reservation_id}/fork")).json()
            assert len(fork["l3_routes"]) == 1
            assert fork["l3_routes"][0]["destination"] == destination

            # Remove the route: cancelling the reservation deprovisions exactly
            # the applied (intent-derived) set (ADR 0014; matches
            # test_l3_intent_execution.py's mock-driver equivalent). A fork
            # save that merely clears intent while the switch stays wired
            # deliberately does NOT do this (addendum X4, "no surprise
            # teardown": proven live while building this test), so cancel is
            # the correct "remove it" path here, not an empty-routes save.
            cancel_resp = await client.delete(f"/reservations/{reservation_id}")
            assert cancel_resp.status_code == 204, cancel_resp.text
            reservation_cancelled = True

            remove_run = await _poll_route_run(client, reservation_id, "remove_route", destination)
            assert remove_run is not None, (
                "the route was never removed (no SUCCESS remove_route run)"
            )
            assert remove_run["status"] == "SUCCESS"

            routes_after_remove = _show_ip_route_static()
            assert destination.split("/")[0] not in routes_after_remove, routes_after_remove
        finally:
            # Defense in depth: remove the route directly if the API path left
            # it behind (e.g. an earlier assertion failed before cleanup ran).
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
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
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
            _seed._make_driver_zip_from_dir(_seed.FRR_L3_DRIVER_DIR),
        )
        switch_template = await _create_template(
            client, driver["id"], f"nos-stack-frr-l3-rej-tmpl-{suffix}"
        )
        dut_driver = await _upload_driver(
            client,
            f"nos-stack-dut-driver-rej-{suffix}",
            "Management",
            _seed._make_dummy_zip("nos-stack-dut-rej"),
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
            assert failed_run["error"], "a FAILED run must carry the device's own rejection text"

            # No SUCCESS run for this destination: the driver must never have
            # reported success for a command the device rejected.
            success_runs = await _runs(client, reservation_id, "configure_route", status="SUCCESS")
            assert destination not in {r.get("port_a") for r in success_runs}

            # Independent verification: the real device never installed it.
            routes_after = _show_ip_route_static()
            assert destination.split("/")[0] not in routes_after, routes_after
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
