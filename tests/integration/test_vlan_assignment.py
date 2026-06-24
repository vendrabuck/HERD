"""Integration tests for fabric-aware VLAN assignment via a mock L2 switch driver.

VLAN provisioning is driven by NATS reservation events to the execution service,
which runs L2 switch driver code in a subprocess sandbox. These tests upload the
checked-in mock L2 driver (drivers/mock_l2), wire a DUT to a Layer 2 Switch
device, reserve the DUT, and assert the resulting create_vlan / add_to_vlan /
delete_vlan operations on the switch via GET /execution/runs.

There is no REST endpoint for VlanAssignment rows, so we assert the observable
downstream effect instead: the driver actually ran the VLAN ops with an
HERD-assigned vlan_id. The run only exists after find_or_assign_vlan created the
ACTIVE assignment, so a SUCCESS create_vlan run is end-to-end proof the
assignment was made; a SUCCESS delete_vlan run is proof it was released.

The suite self-seeds the mock L2 driver via a session fixture, so it no longer
depends on the HERD_VLAN_TEST_DRIVER_ID env gate.
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

_MOCK_L2_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l2"


def _mock_l2_tarball() -> bytes:
    """Package the checked-in drivers/mock_l2 package into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L2_DIR / name, arcname=name)
    return buf.getvalue()


@pytest.fixture(scope="session")
async def l2_driver(base_url, admin_token):
    """Upload the mock Layer 2 Switch driver once per session."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        files = {"file": ("mock_l2.tar.gz", _mock_l2_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l2-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 2 Switch",
            "description": "integration mock L2 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        assert driver["connection_type"] == "Layer 2 Switch"
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l2_template(base_url, admin_token, l2_driver):
    """A device template wired to the mock L2 driver."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        payload = {
            "name": f"mock-l2-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": l2_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL2Switch",
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


async def _create_connection(client, dut_id: str, switch_id: str, switch_port: str) -> dict:
    # The cabling connection_type field is irrelevant to L2 resolution: the
    # execution consumer keys on the far-end device's driver connection_type
    # ("Layer 2 Switch"), not on this field (nats_consumer._resolve_l2_switch_operations).
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


async def _create_reservation(client, device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": [device_id],
            "purpose": "vlan assignment integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


def _vlan_of(run: dict) -> int:
    """Read the HERD-assigned vlan_id from a create_vlan / add_to_vlan run.

    create_execution_run nests the driver method kwargs under
    input_params["method_kwargs"] for queryability (execution_service.py:44-47).
    """
    return run["input_params"]["method_kwargs"]["vlan_id"]


async def _poll_success_runs(
    client, reservation_id: str, action: str, *, timeout: float = 30.0, interval: float = 0.5
) -> list[dict]:
    """Poll GET /execution/runs until a SUCCESS run with `action` appears.

    Returns the list of matching SUCCESS runs (empty on timeout). Provisioning is
    asynchronous (NATS event -> consumer -> driver subprocess), so the test waits
    rather than asserting immediately after the reservation POST returns.
    """
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


async def test_vlan_assigned_on_reservation_create_with_l2_switch(
    admin_client, l2_template, fresh_device
):
    """Reserving a DUT cabled to an L2 switch drives create_vlan + add_to_vlan on
    the switch with an HERD-assigned vlan_id (proof the VlanAssignment was made)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l2_template["id"], f"mock-l2-sw-{suffix}")
    reservation = None
    connection = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "eth1"
        )
        reservation = await _create_reservation(admin_client, fresh_device["id"])

        create_runs = await _poll_success_runs(admin_client, reservation["id"], "create_vlan")
        assert create_runs, "no SUCCESS create_vlan run was recorded for the L2 switch"
        run = create_runs[0]
        assert str(run["device_id"]) == switch["id"]
        vlan_id = _vlan_of(run)
        assert 2 <= vlan_id <= 4094, f"vlan_id {vlan_id} out of the valid 2..4094 range"

        add_runs = await _poll_success_runs(admin_client, reservation["id"], "add_to_vlan")
        assert add_runs, "no SUCCESS add_to_vlan run was recorded for the L2 switch"
        assert str(add_runs[0]["device_id"]) == switch["id"]
        assert _vlan_of(add_runs[0]) == vlan_id
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_vlan_released_on_reservation_cancel(admin_client, l2_template, fresh_device):
    """Cancelling an L2 reservation drives remove_from_vlan + delete_vlan on the
    switch (proof the VlanAssignment was released)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l2_template["id"], f"mock-l2-sw-{suffix}")
    connection = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "eth1"
        )
        reservation = await _create_reservation(admin_client, fresh_device["id"])

        # Provision first, so there is something to release.
        assert await _poll_success_runs(admin_client, reservation["id"], "create_vlan"), (
            "reservation never provisioned a VLAN, cannot test release"
        )

        resp = await admin_client.delete(f"/reservations/{reservation['id']}")
        assert resp.status_code == 204, resp.text

        delete_runs = await _poll_success_runs(admin_client, reservation["id"], "delete_vlan")
        assert delete_runs, "no SUCCESS delete_vlan run was recorded after cancel"
        assert str(delete_runs[0]["device_id"]) == switch["id"]
        remove_runs = await _poll_success_runs(admin_client, reservation["id"], "remove_from_vlan")
        assert remove_runs, "no SUCCESS remove_from_vlan run was recorded after cancel"
    finally:
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_vlan_ids_are_unique_within_same_fabric(admin_client, l2_template, fresh_devices):
    """Two overlapping reservations whose DUTs share one L2 switch (one fabric)
    receive different VLAN ids (the per-fabric uniqueness invariant)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l2_template["id"], f"mock-l2-sw-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    reservations = []
    connections = []
    try:
        connections.append(
            await _create_connection(admin_client, dut_a["id"], switch["id"], "eth1")
        )
        connections.append(
            await _create_connection(admin_client, dut_b["id"], switch["id"], "eth2")
        )

        res_a = await _create_reservation(admin_client, dut_a["id"])
        reservations.append(res_a)
        res_b = await _create_reservation(admin_client, dut_b["id"])
        reservations.append(res_b)

        runs_a = await _poll_success_runs(admin_client, res_a["id"], "create_vlan")
        runs_b = await _poll_success_runs(admin_client, res_b["id"], "create_vlan")
        assert runs_a and runs_b, "both reservations must provision a VLAN on the shared switch"

        vlan_a = _vlan_of(runs_a[0])
        vlan_b = _vlan_of(runs_b[0])
        assert 2 <= vlan_a <= 4094 and 2 <= vlan_b <= 4094
        assert vlan_a != vlan_b, (
            f"two reservations in the same fabric got the same vlan_id {vlan_a}"
        )
    finally:
        for res in reservations:
            await admin_client.delete(f"/reservations/{res['id']}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
