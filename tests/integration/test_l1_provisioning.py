"""Integration tests for Layer 1 cross-connect provisioning via a mock L1 driver.

As of ADR 0009 phase 7 all wiring, initial provisioning included, is fork-driven: the
reservation books a WIRED parent topology (a committed DUT-to-DUT canvas edge), activation
stages a reservation.wiring_changed for the fork's initial version, and the execution
consumer's reconcile resolves the fork's recorded hops through the L1 switch and drives a
single connect_ports(port_a, port_b) pairing the two switch-side ports. Cancelling drives
disconnect_ports from the l1_connection_assignments ledger (phase 6). These tests upload
the checked-in mock L1 driver (drivers/mock_l1), build that topology, and assert the
switch operations via GET /execution/runs.

One test (issue #574) goes further: it wires two same-pair, port-distinct fork edges
(the PR #545 multi-port wiring dialog shape) and asserts execution drives TWO distinct
connect_ports runs with two distinct switch-side port pairs, not just two
fork_connections rows on the cabling side (test_fork_save_port_resolution.py's
existing coverage).

The suite self-seeds the mock L1 driver via a session fixture.
"""

import asyncio
import io
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.asyncio

_MOCK_L1_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l1"


def _mock_l1_tarball() -> bytes:
    """Package the checked-in drivers/mock_l1 package into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L1_DIR / name, arcname=name)
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


@pytest.fixture(scope="session")
async def l1_driver(base_url, admin_token):
    """Upload the mock Layer 1 Switch driver once per session."""
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l1-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 1 Switch",
            "description": "integration mock L1 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        assert driver["connection_type"] == "Layer 1 Switch"
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l1_template(base_url, admin_token, l1_driver):
    """A device template wired to the mock L1 driver."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l1-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": l1_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL1Switch",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        }
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


async def _create_device(client, template_id: str, name: str) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _connect(client, dut_id: str, switch_id: str, switch_port: str) -> dict:
    return await _connect_port(client, dut_id, "eth0", switch_id, switch_port)


async def _connect_port(
    client, dut_id: str, dut_port: str, switch_id: str, switch_port: str
) -> dict:
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": dut_id,
            "port_a": dut_port,
            "device_b_id": switch_id,
            "port_b": switch_port,
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _canvas_edge(a_id: str, b_id: str) -> dict:
    """A committed one-edge canvas wiring device a to device b (React Flow shape)."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": a_id}}},
            {"id": "nB", "data": {"device": {"id": b_id}}},
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


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post("/cabling/topologies", json={"name": f"int-l1-{uuid.uuid4().hex[:8]}"})
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
            "purpose": "l1 provisioning integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _poll_success_runs(
    client, reservation_id: str, action: str, *, timeout: float = 30.0, interval: float = 0.5
) -> list[dict]:
    """Poll GET /execution/runs until a SUCCESS run with `action` appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(
            "/execution/runs",
            params={"reservation_id": reservation_id, "status": "SUCCESS", "limit": 200},
        )
        if resp.status_code == 200:
            matched = [r for r in resp.json().get("items", []) if r["action"] == action]
            if matched:
                return matched
        await asyncio.sleep(interval)
    return []


async def _poll_success_runs_min_count(
    client,
    reservation_id: str,
    action: str,
    min_count: int,
    *,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> list[dict]:
    """Poll GET /execution/runs until at least `min_count` SUCCESS runs with `action`
    are recorded. Unlike `_poll_success_runs` (which returns on the first match), this
    waits for the full expected count so a second, slightly-delayed run is not missed
    and misread as a collapse to fewer wires."""
    deadline = asyncio.get_event_loop().time() + timeout
    matched: list[dict] = []
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(
            "/execution/runs",
            params={"reservation_id": reservation_id, "status": "SUCCESS", "limit": 200},
        )
        if resp.status_code == 200:
            matched = [r for r in resp.json().get("items", []) if r["action"] == action]
            if len(matched) >= min_count:
                return matched
        await asyncio.sleep(interval)
    return matched


def _ports_of(run: dict) -> set:
    """The port pair a connect_ports / disconnect_ports run acted on."""
    kw = run["input_params"]["method_kwargs"]
    return {kw["port_a"], kw["port_b"]}


async def test_l1_ports_connected_on_reservation_create(admin_client, l1_template, fresh_devices):
    """Reserving two DUTs wired DUT-to-DUT in the topology, both cabled to one L1
    switch, drives a single connect_ports on the switch pairing the two switch-side
    ports at activation (ADR 0009 phase 7: the activation-staged wiring_changed
    reconcile resolves the fork's recorded hops; no fork save is needed)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l1_template["id"], f"mock-l1-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    reservation = None
    connections = []
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)

        runs = await _poll_success_runs(admin_client, reservation["id"], "connect_ports")
        assert runs, "no SUCCESS connect_ports run was recorded for the L1 switch"
        assert str(runs[0]["device_id"]) == switch["id"]
        # The pair order depends on hop resolution, so assert the set of ports.
        assert _ports_of(runs[0]) == {"p1", "p2"}
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


def _canvas_two_port_distinct_edges(a_id: str, b_id: str) -> dict:
    """A committed canvas wiring device a to device b with TWO same-pair L1 edges,
    each naming a distinct DUT-side port pair (the PR #545 multi-port wiring dialog
    shape: ``data.source_port_name``/``data.target_port_name`` per edge)."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": a_id}}},
            {"id": "nB", "data": {"device": {"id": b_id}}},
        ],
        "edges": [
            {
                "id": "edge-0",
                "source": "nA",
                "target": "nB",
                "data": {
                    "layer": "L1",
                    "isProposal": False,
                    "source_port_name": "eth1",
                    "target_port_name": "eth1",
                },
            },
            {
                "id": "edge-1",
                "source": "nA",
                "target": "nB",
                "data": {
                    "layer": "L1",
                    "isProposal": False,
                    "source_port_name": "eth2",
                    "target_port_name": "eth2",
                },
            },
        ],
    }


async def _wiring_status(client, reservation_id: str) -> dict:
    resp = await client.get(f"/reservations/{reservation_id}/wiring-status")
    resp.raise_for_status()
    return resp.json()


async def _poll_wiring_status(
    client, reservation_id: str, predicate, *, timeout: float = 20.0, interval: float = 0.5
) -> dict | None:
    """Poll wiring-status until `predicate(status)` is true; return the last status seen."""
    deadline = asyncio.get_event_loop().time() + timeout
    status: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        status = await _wiring_status(client, reservation_id)
        if predicate(status):
            return status
        await asyncio.sleep(interval)
    return status or None


async def test_l1_two_port_distinct_edges_drive_two_distinct_connect_calls(
    admin_client, l1_template, fresh_devices
):
    """Two same-pair fork edges naming distinct ports (issue #574, guarding the PR #545
    N-wires guarantee through execution) drive TWO distinct connect_ports runs on the L1
    switch, one per physical port pair, not one collapsed call.

    Each DUT is cabled to the switch on two ports (eth1/eth2), landing on four distinct
    switch ports, so the two canvas edges resolve to two disjoint switch-side pairs:
    {p1, p2} for the eth1-eth1 edge and {p3, p4} for the eth2-eth2 edge. A regression that
    collapses both edges to a single hop (the #531 failure mode, one layer lower) would
    either produce only one SUCCESS connect_ports run or two runs sharing a port, both of
    which the pair-distinctness assertion below catches.
    """
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l1_template["id"], f"mock-l1-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    reservation = None
    connections = []
    topology_id = None
    try:
        connections.append(
            await _connect_port(admin_client, dut_a["id"], "eth1", switch["id"], "p1")
        )
        connections.append(
            await _connect_port(admin_client, dut_b["id"], "eth1", switch["id"], "p2")
        )
        connections.append(
            await _connect_port(admin_client, dut_a["id"], "eth2", switch["id"], "p3")
        )
        connections.append(
            await _connect_port(admin_client, dut_b["id"], "eth2", switch["id"], "p4")
        )
        topology_id = await _create_topology(
            admin_client, _canvas_two_port_distinct_edges(dut_a["id"], dut_b["id"])
        )
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]

        # Vacuity guard: the fork itself must have resolved two distinct port pairs
        # before asking anything of execution, so a silent cabling-side collapse to
        # one wire fails HERE with a clear message rather than being masked by a
        # weak execution-side assertion. With an intermediate L1 switch, each canvas
        # edge resolves to TWO hops (DUT-to-switch on each side), so four
        # fork_connections rows are expected in total, grouped by edge_key into two
        # pairs of hops, one per canvas edge.
        fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert fork.status_code == 200, fork.text
        fork_connections = fork.json()["connections"]
        assert len(fork_connections) == 4, (
            "expected four fork_connections rows (two hops per port-distinct canvas "
            f"edge), got {len(fork_connections)}: {fork_connections}"
        )
        by_edge: dict[str, list[dict]] = {}
        for conn in fork_connections:
            by_edge.setdefault(conn["edge_key"], []).append(conn)
        assert set(by_edge) == {"edge-0", "edge-1"}, (
            f"expected hops grouped under edge-0 and edge-1, got {set(by_edge)}: {fork_connections}"
        )
        for edge_key, expected_dut_port, expected_switch_ports in (
            ("edge-0", "eth1", {"p1", "p2"}),
            ("edge-1", "eth2", {"p3", "p4"}),
        ):
            hops = by_edge[edge_key]
            assert len(hops) == 2, f"{edge_key} expected two hops, got {hops}"
            dut_ports = {
                port
                for c in hops
                for dev, port in ((c["device_a_id"], c["port_a"]), (c["device_b_id"], c["port_b"]))
                if dev in (dut_a["id"], dut_b["id"])
            }
            switch_ports = {
                port
                for c in hops
                for dev, port in ((c["device_a_id"], c["port_a"]), (c["device_b_id"], c["port_b"]))
                if dev == switch["id"]
            }
            assert dut_ports == {expected_dut_port}, (
                f"{edge_key} expected both hops to land on DUT port "
                f"{expected_dut_port!r}, got {dut_ports}: {hops}"
            )
            assert switch_ports == expected_switch_ports, (
                f"{edge_key} expected switch-side ports {expected_switch_ports}, "
                f"got {switch_ports}: {hops}"
            )

        # Execution boundary: two SUCCESS connect_ports runs, with distinct switch-side
        # port pairs. A collapse to one hop leaves only one run; a mispair leaves two
        # runs sharing a port instead of the expected {p1,p2}/{p3,p4} split.
        runs = await _poll_success_runs_min_count(
            admin_client, reservation_id, "connect_ports", 2, timeout=10.0
        )
        assert len(runs) == 2, f"expected two SUCCESS connect_ports runs, got {len(runs)}: {runs}"
        assert all(str(r["device_id"]) == switch["id"] for r in runs)
        run_pairs = [_ports_of(r) for r in runs]
        assert {"p1", "p2"} in run_pairs, f"the eth1-eth1 pair was never driven: {run_pairs}"
        assert {"p3", "p4"} in run_pairs, f"the eth2-eth2 pair was never driven: {run_pairs}"
        assert run_pairs[0] != run_pairs[1], (
            f"both connect_ports runs drove the SAME port pair: {run_pairs}"
        )

        # The layered wiring-status surface must also show two ACTIVE l1 rows with the
        # same two distinct port pairs, not just two execution runs.
        status = await _poll_wiring_status(
            admin_client,
            reservation_id,
            lambda s: len([c for c in s["connections"] if c["status"] == "ACTIVE"]) >= 2,
            timeout=5.0,
        )
        assert status is not None, "wiring-status never reported two ACTIVE l1 rows"
        active_l1 = [
            c for c in status["connections"] if c["layer"] == "l1" and c["status"] == "ACTIVE"
        ]
        assert len(active_l1) == 2, (
            f"expected two ACTIVE l1 wiring-status rows, got {len(active_l1)}: {active_l1}"
        )
        status_pairs = [{c["port_a"], c["port_b"]} for c in active_l1]
        assert {"p1", "p2"} in status_pairs
        assert {"p3", "p4"} in status_pairs
        assert status_pairs[0] != status_pairs[1], (
            f"both ACTIVE l1 rows carry the SAME port pair: {status_pairs}"
        )

        # Baseline restore: cancel and confirm teardown released both wires, driving two
        # distinct disconnect_ports runs for the same two port pairs.
        cancelled = await admin_client.delete(f"/reservations/{reservation_id}")
        assert cancelled.status_code == 204, cancelled.text
        reservation = None

        disconnect_runs = await _poll_success_runs_min_count(
            admin_client, reservation_id, "disconnect_ports", 2, timeout=10.0
        )
        assert len(disconnect_runs) == 2, (
            f"expected two SUCCESS disconnect_ports runs, got "
            f"{len(disconnect_runs)}: {disconnect_runs}"
        )
        disconnect_pairs = [_ports_of(r) for r in disconnect_runs]
        assert {"p1", "p2"} in disconnect_pairs
        assert {"p3", "p4"} in disconnect_pairs

        released_status = await _poll_wiring_status(
            admin_client,
            reservation_id,
            lambda s: all(c["status"] == "RELEASED" for c in s["connections"]),
            timeout=5.0,
        )
        assert released_status is not None, "wiring never fully released after cancel"
        final_l1 = [c for c in released_status["connections"] if c["layer"] == "l1"]
        assert len(final_l1) == 2
        assert all(c["status"] == "RELEASED" for c in final_l1), final_l1
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l1_ports_disconnected_on_reservation_cancel(
    admin_client, l1_template, fresh_devices
):
    """Cancelling the reservation drives disconnect_ports on the switch for the
    same port pair, released from the l1_connection_assignments ledger the
    activation-staged reconcile recorded (phase 6 teardown over phase 7 wiring)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l1_template["id"], f"mock-l1-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)

        assert await _poll_success_runs(admin_client, reservation["id"], "connect_ports"), (
            "reservation never connected the ports, cannot test disconnect"
        )

        resp = await admin_client.delete(f"/reservations/{reservation['id']}")
        assert resp.status_code == 204, resp.text

        runs = await _poll_success_runs(admin_client, reservation["id"], "disconnect_ports")
        assert runs, "no SUCCESS disconnect_ports run was recorded after cancel"
        assert str(runs[0]["device_id"]) == switch["id"]
        assert _ports_of(runs[0]) == {"p1", "p2"}
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
