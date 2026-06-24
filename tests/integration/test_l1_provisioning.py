"""Integration tests for Layer 1 cross-connect provisioning via a mock L1 driver.

Reserving two DUTs that are cabled to the same Layer 1 Switch device drives a
single connect_ports(port_a, port_b) on the switch (the consumer pairs the two
switch-side ports), and cancelling drives disconnect_ports. These tests upload
the checked-in mock L1 driver (drivers/mock_l1), build that topology, and assert
the switch operations via GET /execution/runs.

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


async def _reserve(client, device_ids: list[str]) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
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


def _ports_of(run: dict) -> set:
    """The port pair a connect_ports / disconnect_ports run acted on."""
    kw = run["input_params"]["method_kwargs"]
    return {kw["port_a"], kw["port_b"]}


async def test_l1_ports_connected_on_reservation_create(admin_client, l1_template, fresh_devices):
    """Reserving two DUTs cabled to one L1 switch drives a single connect_ports on
    the switch pairing the two switch-side ports."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l1_template["id"], f"mock-l1-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    reservation = None
    connections = []
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]])

        runs = await _poll_success_runs(admin_client, reservation["id"], "connect_ports")
        assert runs, "no SUCCESS connect_ports run was recorded for the L1 switch"
        assert str(runs[0]["device_id"]) == switch["id"]
        # The pair order depends on device iteration, so assert the set of ports.
        assert _ports_of(runs[0]) == {"p1", "p2"}
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l1_ports_disconnected_on_reservation_cancel(
    admin_client, l1_template, fresh_devices
):
    """Cancelling the reservation drives disconnect_ports on the switch for the
    same port pair."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l1_template["id"], f"mock-l1-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    try:
        connections.append(await _connect(admin_client, dut_a["id"], switch["id"], "p1"))
        connections.append(await _connect(admin_client, dut_b["id"], switch["id"], "p2"))
        reservation = await _reserve(admin_client, [dut_a["id"], dut_b["id"]])

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
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
