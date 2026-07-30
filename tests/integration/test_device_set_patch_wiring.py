"""End-to-end pin of ADR 0009 Decision 6: the device-set PATCH drives no wiring.

The one user-visible semantics change of the ADR 0009 phase 7 unification: adding
a device to a reservation via the device-set PATCH wires NOTHING (the PATCH keeps
only its dynamic-instance and health-tier roles); the device's wiring comes from
the fork, so it is wired exactly when a fork save draws its connections. This
suite proves both halves live:

1. PATCH-add produces zero driver runs. Deterministic via the ordering anchor
   pattern: the execution consumer processes the reservations stream
   sequentially, so once the wiring_changed staged by the LATER fork save has
   provisioned, the PATCH's reservation.updated was provably consumed, and any
   wiring it drove would already be visible in /execution/runs.
2. The subsequent fork save that draws the added device's edge is what connects
   it (a connect_ports SUCCESS on the switch, and exactly one such run in total).

Self-seeds the mock L1 driver (session fixtures), per the integration-test
seeding convention.
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
async def patch_l1_driver(base_url, admin_token):
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l1-d6-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 1 Switch",
            "description": "Decision 6 device-set PATCH wiring test driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def patch_l1_template(base_url, admin_token, patch_l1_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l1-d6-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": patch_l1_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL1SwitchDecision6",
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


def _canvas(device_ids: list[str], edges: list[tuple[str, str]]) -> dict:
    node_ids = {device_id: f"n{i}" for i, device_id in enumerate(device_ids)}
    return {
        "nodes": [
            {"id": node_ids[device_id], "data": {"device": {"id": device_id}}}
            for device_id in device_ids
        ],
        "edges": [
            {
                "id": f"e{i}",
                "source": node_ids[a_id],
                "target": node_ids[b_id],
                "data": {"layer": "L1", "isProposal": False},
            }
            for i, (a_id, b_id) in enumerate(edges, start=1)
        ],
    }


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post("/cabling/topologies", json={"name": f"int-d6-{uuid.uuid4().hex[:8]}"})
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
            "purpose": "Decision 6 device-set PATCH wiring test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _poll_active(client, reservation_id: str, *, timeout: float = 20.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _runs(client, reservation_id: str, action: str, status: str = "SUCCESS") -> list[dict]:
    resp = await client.get(
        "/execution/runs",
        params={"reservation_id": reservation_id, "status": status, "limit": 200},
    )
    resp.raise_for_status()
    return [r for r in resp.json().get("items", []) if r["action"] == action]


async def _poll_runs(
    client, reservation_id: str, action: str, *, timeout: float = 30.0
) -> list[dict]:
    deadline = asyncio.get_event_loop().time() + timeout
    matched: list[dict] = []
    while asyncio.get_event_loop().time() < deadline:
        matched = await _runs(client, reservation_id, action)
        if matched:
            return matched
        await asyncio.sleep(0.5)
    return matched


async def test_patch_add_wires_nothing_until_fork_save(
    admin_client, patch_l1_template, fresh_devices
):
    """Decision 6 end to end: a device-set PATCH add drives zero wiring; the
    subsequent fork save that draws the device's edge connects it."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, patch_l1_template["id"], f"mock-l1-d6-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    reservation = None
    connections = []
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))

        # Book dut_a alone against an EDGELESS topology: the fork exists from
        # activation but its intended set is empty, so activation wires nothing.
        topology_id = await _create_topology(admin_client, _canvas([dut_a["id"]], []))
        reservation = await _reserve(admin_client, [dut_a["id"]], topology_id)
        res_id = reservation["id"]
        assert await _poll_active(admin_client, res_id), "reservation never activated"

        # Add dut_b via the device-set PATCH. Decision 6: this reserves the device
        # (dynamic/health-tier roles) but wires NOTHING.
        resp = await admin_client.patch(
            f"/reservations/{res_id}",
            json={"device_ids": [dut_a["id"], dut_b["id"]]},
        )
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["device_ids"]) == sorted([dut_a["id"], dut_b["id"]])

        # Draw the added device's edge on the fork and save: THIS is what wires it.
        save = await admin_client.post(
            f"/reservations/{res_id}/fork/save",
            json={"canvas_data": _canvas([dut_a["id"], dut_b["id"]], [(dut_a["id"], dut_b["id"])])},
        )
        assert save.status_code == 200, save.text

        runs = await _poll_runs(admin_client, res_id, "connect_ports")
        assert runs, "the fork save never connected the added device"
        assert str(runs[0]["device_id"]) == switch["id"]
        kw = runs[0]["input_params"]["method_kwargs"]
        assert {kw["port_a"], kw["port_b"]} == {"p1", "p2"}

        # Ordering anchor: the save's wiring_changed was staged AFTER the PATCH's
        # reservation.updated, and the consumer processes the stream sequentially,
        # so the PATCH event is provably consumed by now. Exactly one connect_ports
        # exists: the save's. The PATCH added none.
        assert len(await _runs(admin_client, res_id, "connect_ports")) == 1, (
            "the device-set PATCH must not wire an added device (ADR 0009 Decision 6)"
        )
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection in connections:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
