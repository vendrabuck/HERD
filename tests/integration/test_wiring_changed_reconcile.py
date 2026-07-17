"""Integration coverage for the connection-driven L1 reconcile (ADR 0007, #345 P3b phase 3).

End-to-end over a running HERD stack with the checked-in mock L1 driver:

- The save flow: reserve and activate two DUTs cabled through an L1 switch (a
  connect_ports fires on activation), then save an emptied fork canvas. The save
  stages reservation.wiring_changed; the execution consumer reconciles the fork's
  now-empty intended set against the ACTIVE cross-connect and drives a
  disconnect_ports on the switch. Asserted through GET /execution/runs, the
  observable proxy for the l1_connection_assignments flip (the per-connection
  wiring-status endpoint is phase 4).
- The frozen guard (Decision 7): after the reservation completes (teardown freezes
  the wiring state), a directly-injected stale wiring_changed must not reconnect.
- Replay idempotency (Decision 4): a stale wiring_changed (fork_version at or below
  last-applied) injected after a save is a no-op, driving no additional switch op.

Assignment-row and FAILED-row assertions at the table level are not reachable through
the API in phase 3 (the wiring-status endpoint lands in phase 4), so these tests assert
the driver's observable execution runs. They require a running stack and NATS
reachable from the host; without either they skip or error at connect time, which is
expected. Live-gated at review.
"""

import asyncio
import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ._nats_helpers import probe_nats, publish_raw

pytestmark = pytest.mark.asyncio

_MOCK_L1_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l1"
_WIRING_CHANGED_SUBJECT = "herd.reservations.wiring_changed"


def _mock_l1_tarball() -> bytes:
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
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l1-wc-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 1 Switch",
            "description": "wiring_changed integration mock L1 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l1_template(base_url, admin_token, l1_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l1-wc-tmpl-{uuid.uuid4().hex[:8]}",
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


async def _create_switch(client, template_id: str) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": f"mock-l1-sw-{uuid.uuid4().hex[:8]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _connect(client, dut_id: str, switch_id: str, switch_port: str) -> dict:
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


def _canvas_edge(a_id: str, b_id: str) -> dict:
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
    resp = await client.post("/cabling/topologies", json={"name": f"int-wc-{uuid.uuid4().hex[:8]}"})
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_ids: list[str], topology_id: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "wiring_changed integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if topology_id is not None:
        body["topology_id"] = topology_id
    resp = await client.post("/reservations/", json=body)
    resp.raise_for_status()
    return resp.json()


async def _count_success_runs(client, reservation_id: str, action: str) -> int:
    resp = await client.get(
        "/execution/runs",
        params={"reservation_id": reservation_id, "status": "SUCCESS", "limit": 200},
    )
    if resp.status_code != 200:
        return 0
    return len([r for r in resp.json().get("items", []) if r["action"] == action])


async def _poll_run_count_at_least(
    client, reservation_id: str, action: str, target: int, *, timeout: float = 25.0
) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    count = 0
    while asyncio.get_event_loop().time() < deadline:
        count = await _count_success_runs(client, reservation_id, action)
        if count >= target:
            return count
        await asyncio.sleep(0.5)
    return count


async def test_fork_save_release_drives_disconnect(admin_client, l1_template, fresh_devices):
    """Emptying and saving the fork releases the ACTIVE cross-connect: the consumer
    reconciles the now-empty intended set and drives disconnect_ports on the switch."""
    switch = await _create_switch(admin_client, l1_template["id"])
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]

        # Activation drives the initial connect_ports.
        assert await _poll_run_count_at_least(admin_client, reservation_id, "connect_ports", 1), (
            "activation never connected the ports"
        )

        # Save an emptied fork canvas: cabling releases the wire and stages
        # reservation.wiring_changed; the consumer reconciles to an empty set.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )
        assert saved.status_code == 200, saved.text

        # The reconcile drives a disconnect_ports on the switch for the freed pair.
        disconnects = await _poll_run_count_at_least(
            admin_client, reservation_id, "disconnect_ports", 1
        )
        assert disconnects >= 1, "wiring_changed reconcile did not disconnect the released pair"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_wiring_changed_frozen_after_complete_no_reconnect(
    admin_client, l1_template, fresh_devices
):
    """After the reservation completes (teardown freezes the wiring state), an injected
    stale wiring_changed does not reconnect: the frozen guard is a no-op before any
    driver call (ADR 0007 Decision 7)."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(f"NATS not reachable from host: {nats_err}")

    switch = await _create_switch(admin_client, l1_template["id"])
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]])
        reservation_id = reservation["id"]

        assert await _poll_run_count_at_least(admin_client, reservation_id, "connect_ports", 1)

        # Complete the reservation: teardown disconnects and freezes the wiring state.
        released = await admin_client.put(f"/reservations/{reservation_id}/release")
        assert released.status_code == 200, released.text
        await _poll_run_count_at_least(admin_client, reservation_id, "disconnect_ports", 1)

        connect_before = await _count_success_runs(admin_client, reservation_id, "connect_ports")

        # Inject a stale wiring_changed asking to rebuild the pair. Frozen => no-op.
        payload = {
            "event": "reservation.wiring_changed",
            "reservation_id": reservation_id,
            "fork_version": 99,
            "released": [],
            "built": [
                {
                    "device_a_id": dut_a["id"],
                    "port_a": "eth0",
                    "device_b_id": switch["id"],
                    "port_b": "p1",
                    "layer": "L1",
                    "physical_connection_id": None,
                },
                {
                    "device_a_id": switch["id"],
                    "port_a": "p2",
                    "device_b_id": dut_b["id"],
                    "port_b": "eth0",
                    "layer": "L1",
                    "physical_connection_id": None,
                },
            ],
            "event_id": str(uuid.uuid4()),
        }
        await publish_raw(_WIRING_CHANGED_SUBJECT, json.dumps(payload).encode())

        # Give the consumer time to (not) act, then assert no new connect fired.
        await asyncio.sleep(6)
        connect_after = await _count_success_runs(admin_client, reservation_id, "connect_ports")
        assert connect_after == connect_before, "frozen reservation must not reconnect"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_wiring_changed_stale_replay_no_double_apply(
    admin_client, l1_template, fresh_devices
):
    """A stale wiring_changed (fork_version at or below last-applied) injected after a
    save reconcile is a no-op: no additional switch op fires (ADR 0007 Decision 4)."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(f"NATS not reachable from host: {nats_err}")

    switch = await _create_switch(admin_client, l1_template["id"])
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    reservation_id = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]], topology_id)
        reservation_id = reservation["id"]

        assert await _poll_run_count_at_least(admin_client, reservation_id, "connect_ports", 1)

        # Save an emptied canvas to release the pair and advance last-applied.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )
        assert saved.status_code == 200, saved.text
        await _poll_run_count_at_least(admin_client, reservation_id, "disconnect_ports", 1)

        disconnect_before = await _count_success_runs(
            admin_client, reservation_id, "disconnect_ports"
        )

        # Inject a stale wiring_changed (version 1, below the applied version). The
        # last-applied guard skips it: no re-disconnect and no reconnect.
        payload = {
            "event": "reservation.wiring_changed",
            "reservation_id": reservation_id,
            "fork_version": 1,
            "released": [],
            "built": [],
            "event_id": str(uuid.uuid4()),
        }
        await publish_raw(_WIRING_CHANGED_SUBJECT, json.dumps(payload).encode())

        await asyncio.sleep(6)
        disconnect_after = await _count_success_runs(
            admin_client, reservation_id, "disconnect_ports"
        )
        connect_after = await _count_success_runs(admin_client, reservation_id, "connect_ports")
        assert disconnect_after == disconnect_before, "stale replay must not re-disconnect"
        # Exactly the one activation connect; the stale event drives no rebuild.
        assert connect_after == 1, "stale replay must not reconnect"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
