"""Integration coverage for the ledger-driven terminal teardown (ADR 0009 phase 6, #416).

End-to-end over a running HERD stack with the checked-in mock L1/L2/L3 drivers. A terminal
transition (here, cancel) releases a reservation's applied wiring from the three ledgers,
not the device set:

- Full three-layer teardown: activate a reservation whose fork wires three independent legs
  (a DUT pair through an L1 switch, a DUT pair through an L2 switch, a DUT pair through an L3
  switch), so a fork save builds an L1 cross-connect, an L2 VLAN membership per switch port,
  and the L3 switch's pinned routes. Cancelling the reservation must drive disconnect_ports,
  remove_from_vlan, and remove_route (the release of each ledger), and leave the L1
  wiring-status RELEASED with no FAILED residue. Asserted through GET /execution/runs (the
  observable proxy for the ledger flips; the layered wiring-status surface is phase 8) plus
  the L1 wiring-status endpoint.
- Failed teardown converges via terminal-status retry (the phase-3 direction-scoped retry,
  now exercised on the ledger-driven teardown path): cancel with the L2 switch's
  remove_from_vlan knob armed lands a FAILED membership row intended RELEASED; clearing the
  knob and hitting POST /reservations/{id}/wiring/retry on the CANCELLED reservation
  converges it "released".

Requires a running stack with NATS reachable from the host; without either it skips or
errors at connect time, which is expected. Live-gated at review.
"""

import asyncio
import io
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ._nats_helpers import probe_nats

pytestmark = pytest.mark.asyncio

_DRIVERS = Path(__file__).resolve().parents[2] / "drivers"

# The routes the L3 switch's config version declares, and therefore the pinned set.
ROUTES = [
    {"destination": "10.20.0.0/24", "next_hop": "192.0.2.1", "interface": "eth0"},
    {"destination": "10.21.0.0/24", "interface": "eth1"},
]


def _tarball(driver_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(driver_dir / name, arcname=name)
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


async def _upload_driver(client, subdir: str, connection_type: str) -> dict:
    files = {"file": (f"{subdir}.tar.gz", _tarball(_DRIVERS / subdir), "application/gzip")}
    data = {
        "name": f"{subdir}-teardown-{uuid.uuid4().hex[:8]}",
        "connection_type": connection_type,
        "description": f"ledger teardown integration mock {connection_type}",
    }
    resp = await client.post("/inventory/drivers", files=files, data=data)
    resp.raise_for_status()
    return resp.json()


async def _create_template(client, driver_id: str, model: str) -> dict:
    payload = {
        "name": f"{model}-teardown-tmpl-{uuid.uuid4().hex[:8]}",
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "IntegrationVendor",
        "model": model,
        "sections": [
            {
                "name": "General",
                "fields": [
                    {"key": "model", "label": "Model", "type": "string"},
                    {"key": "mock_fail_actions", "label": "Mock fail", "type": "string"},
                ],
            }
        ],
    }
    resp = await client.post("/inventory/templates", json=payload)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="session")
async def teardown_drivers(base_url, admin_token):
    """Upload the L1/L2/L3 mock drivers + templates once; clean up after the session."""
    async with _admin_session_client(base_url, admin_token) as client:
        l1_d = await _upload_driver(client, "mock_l1", "Layer 1 Switch")
        l2_d = await _upload_driver(client, "mock_l2", "Layer 2 Switch")
        l3_d = await _upload_driver(client, "mock_l3", "Layer 3 Switch")
        l1_t = await _create_template(client, l1_d["id"], "MockL1Teardown")
        l2_t = await _create_template(client, l2_d["id"], "MockL2Teardown")
        l3_t = await _create_template(client, l3_d["id"], "MockL3Teardown")
        yield {"l1": l1_t, "l2": l2_t, "l3": l3_t}
        for t in (l1_t, l2_t, l3_t):
            await client.delete(f"/inventory/templates/{t['id']}")
        for d in (l1_d, l2_d, l3_d):
            await client.delete(f"/inventory/drivers/{d['id']}")


async def _create_switch(client, template_id: str, name: str, field_data: dict) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": f"{name}-{uuid.uuid4().hex[:8]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": field_data,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _set_l3_config(client, switch_id: str) -> None:
    resp = await client.post(
        f"/inventory/devices/{switch_id}/config-versions",
        json={"config": {"routes": ROUTES}, "description": "ledger teardown routes"},
    )
    resp.raise_for_status()


async def _connect(client, dut_id, dut_port, switch_id, switch_port) -> dict:
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


def _node(dev_id):
    return {"id": f"n{dev_id[:8]}", "data": {"device": {"id": dev_id}}}


def _edge(idx, a_id, b_id):
    return {
        "id": f"e{idx}",
        "source": f"n{a_id[:8]}",
        "target": f"n{b_id[:8]}",
        "data": {"layer": "L1", "isProposal": False},
    }


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-teardown-{uuid.uuid4().hex[:8]}"}
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
            "purpose": "ledger teardown integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
            "topology_id": topology_id,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _save_fork(client, reservation_id, canvas):
    return await client.post(
        f"/reservations/{reservation_id}/fork/save", json={"canvas_data": canvas}
    )


async def _count_runs(client, reservation_id, action, status="SUCCESS") -> int:
    resp = await client.get(
        "/execution/runs",
        params={"reservation_id": reservation_id, "status": status, "limit": 400},
    )
    if resp.status_code != 200:
        return 0
    return len([r for r in resp.json().get("items", []) if r["action"] == action])


async def _poll_runs(client, reservation_id, action, target=1, *, timeout=30.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    count = 0
    while asyncio.get_event_loop().time() < deadline:
        count = await _count_runs(client, reservation_id, action)
        if count >= target:
            return count
        await asyncio.sleep(0.5)
    return count


async def _poll_active(client, reservation_id, *, timeout=15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _wiring_status(client, reservation_id) -> dict:
    resp = await client.get(f"/reservations/{reservation_id}/wiring-status")
    resp.raise_for_status()
    return resp.json()


async def _poll_wiring_conn(client, reservation_id, predicate, *, timeout=25.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for conn in (await _wiring_status(client, reservation_id)).get("connections", []):
            if predicate(conn):
                return conn
        await asyncio.sleep(0.5)
    return None


async def _poll_retry_l2(client, reservation_id, port, outcome, *, timeout=25.0):
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.post(f"/reservations/{reservation_id}/wiring/retry")
        if resp.status_code == 200:
            for row in resp.json().get("results", []):
                if row.get("layer") == "l2" and row.get("port") == port:
                    last = row
                    if row.get("outcome") == outcome:
                        return row
        await asyncio.sleep(0.5)
    return last


async def test_cancel_releases_all_three_ledgers(
    base_url, admin_token, admin_client, fresh_devices, teardown_drivers
):
    """A reservation wired through L1+L2+L3 switches via a fork save releases every ledger on
    cancel: disconnect_ports, remove_from_vlan, and remove_route all fire, and the L1
    wiring-status ends RELEASED with no FAILED residue."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)

    a1, a2, b1, b2, c1, c2 = await fresh_devices(6)
    l1_sw = await _create_switch(admin_client, teardown_drivers["l1"]["id"], "l1sw", {"model": "t"})
    l2_sw = await _create_switch(admin_client, teardown_drivers["l2"]["id"], "l2sw", {"model": "t"})
    l3_sw = await _create_switch(admin_client, teardown_drivers["l3"]["id"], "l3sw", {"model": "t"})
    switches = [l1_sw, l2_sw, l3_sw]
    connections = []
    rid = None
    topology_id = None
    try:
        await _set_l3_config(admin_client, l3_sw["id"])
        # L1 leg (a1-L1-a2), L2 leg (b1-L2-b2), L3 leg (c1-L3-c2).
        connections.append(await _connect(admin_client, a1["id"], "eth0", l1_sw["id"], "p1"))
        connections.append(await _connect(admin_client, a2["id"], "eth0", l1_sw["id"], "p2"))
        connections.append(await _connect(admin_client, b1["id"], "eth0", l2_sw["id"], "1"))
        connections.append(await _connect(admin_client, b2["id"], "eth0", l2_sw["id"], "2"))
        connections.append(await _connect(admin_client, c1["id"], "eth0", l3_sw["id"], "1"))
        connections.append(await _connect(admin_client, c2["id"], "eth0", l3_sw["id"], "2"))

        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        device_ids = [d["id"] for d in (a1, a2, b1, b2, c1, c2)] + [s["id"] for s in switches]
        res = await _reserve(admin_client, device_ids, topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        # Fork save that wires all three legs: L1 builds the cross-connect, L2 joins the
        # VLAN, L3 pins and configures the routes.
        canvas = {
            "nodes": [_node(d["id"]) for d in (a1, a2, b1, b2, c1, c2)],
            "edges": [
                _edge(1, a1["id"], a2["id"]),
                _edge(2, b1["id"], b2["id"]),
                _edge(3, c1["id"], c2["id"]),
            ],
        }
        saved = await _save_fork(admin_client, rid, canvas)
        assert saved.status_code == 200, saved.text
        assert await _poll_runs(admin_client, rid, "connect_ports", 1), "L1 never built"
        assert await _poll_runs(admin_client, rid, "add_to_vlan", 1), "L2 membership never joined"
        assert await _poll_runs(admin_client, rid, "configure_route", 1), "L3 routes never pinned"

        # Cancel: the ledger-driven teardown releases every layer.
        cancelled = await admin_client.delete(f"/reservations/{rid}")
        assert cancelled.status_code == 204, cancelled.text

        assert await _poll_runs(admin_client, rid, "disconnect_ports", 1), "L1 never disconnected"
        assert await _poll_runs(admin_client, rid, "remove_from_vlan", 1), "L2 never left the VLAN"
        assert await _poll_runs(admin_client, rid, "remove_route", 1), "L3 routes never removed"

        # The L1 wiring-status ends RELEASED with no FAILED residue.
        released = await _poll_wiring_conn(
            admin_client, rid, lambda c: c["status"] == "RELEASED", timeout=25.0
        )
        assert released is not None, "the L1 cross-connect never reached RELEASED"
        remaining = [
            c
            for c in (await _wiring_status(admin_client, rid))["connections"]
            if c["status"] == "FAILED"
        ]
        assert remaining == [], "no FAILED wiring row should remain after a clean teardown"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        for sw in switches:
            await admin_client.delete(f"/inventory/devices/{sw['id']}")


async def test_cancel_teardown_failure_converges_on_terminal_retry(
    base_url, admin_token, admin_client, fresh_devices, teardown_drivers
):
    """A cancel whose L2 remove_from_vlan is forced to fail lands a FAILED membership row
    intended RELEASED; the terminal-status manual retry (phase-3 direction-scoped, now on the
    ledger-driven teardown path) converges it "released" once the knob is cleared."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)

    b1, b2 = await fresh_devices(2)
    l2_sw = await _create_switch(admin_client, teardown_drivers["l2"]["id"], "l2sw", {"model": "t"})
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, b1["id"], "eth0", l2_sw["id"], "1"))
        connections.append(await _connect(admin_client, b2["id"], "eth0", l2_sw["id"], "2"))
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [b1["id"], b2["id"], l2_sw["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        canvas = {
            "nodes": [_node(b1["id"]), _node(b2["id"])],
            "edges": [_edge(1, b1["id"], b2["id"])],
        }
        saved = await _save_fork(admin_client, rid, canvas)
        assert saved.status_code == 200, saved.text
        assert await _poll_runs(admin_client, rid, "add_to_vlan", 1), "L2 membership never joined"

        # Arm the remove knob, then cancel: the ledger teardown's remove_from_vlan fails and
        # parks a FAILED membership row intended RELEASED.
        armed = await admin_client.put(
            f"/inventory/devices/{l2_sw['id']}",
            json={"field_data": {"model": "t", "mock_fail_actions": "remove_from_vlan"}},
        )
        assert armed.status_code == 200, armed.text
        cancelled = await admin_client.delete(f"/reservations/{rid}")
        assert cancelled.status_code == 204, cancelled.text

        failed = await _poll_retry_l2(admin_client, rid, "1", "still_failed")
        assert failed is not None and failed["layer"] == "l2", "no FAILED L2 release row surfaced"

        # Clear the knob and retry on the CANCELLED reservation: the release converges.
        cleared = await admin_client.put(
            f"/inventory/devices/{l2_sw['id']}",
            json={"field_data": {"model": "t", "mock_fail_actions": ""}},
        )
        assert cleared.status_code == 200, cleared.text
        released = await _poll_retry_l2(admin_client, rid, "1", "released")
        assert released is not None, "the L2 release never converged after clearing the knob"
        assert released["outcome"] == "released"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{l2_sw['id']}")
