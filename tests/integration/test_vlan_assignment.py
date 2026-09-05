"""Integration tests for fabric-aware VLAN assignment via a mock L2 switch driver.

As of ADR 0009 phase 7 all wiring, initial provisioning included, is fork-driven: a
reservation books a WIRED parent topology, activation stages a reservation.wiring_changed
for the fork's initial version, and the execution consumer's layered reconcile derives L2
membership from the fork's recorded hops and drives add_to_vlan on the switch. No fork
save is needed; activation provisions the initial wiring directly. These tests upload the
checked-in mock L2 driver (drivers/mock_l2), wire a DUT to a Layer 2 Switch device on
both the physical connection graph and the topology canvas, reserve the DUT with that
topology, and assert the resulting add_to_vlan / remove_from_vlan operations on the
switch via GET /execution/runs.

The VLAN-definition lifecycle is HERD-owned as of issue #442 (Option B, decided
2026-08-01): the VLAN number stays a per-fabric DATABASE allocation
(find_or_assign_vlan), and the allocation transitions now drive the switch-side
definition too: create_vlan runs when the allocation first gains a built membership
(before the first add_to_vlan per switch in the transit-inclusive scope), and
delete_vlan runs on last-free per defined switch, supersession-guarded. These pins
deliberately FLIPPED from the phase 6/7 membership-only vocabulary when #442 landed.

There is no REST endpoint for VlanAssignment rows, so we assert the observable
downstream effect instead: the driver actually ran the definition and membership ops
with an HERD-assigned vlan_id. The run only exists after find_or_assign_vlan created
the ACTIVE allocation, so a SUCCESS add_to_vlan run carrying the assigned vlan_id is
end-to-end proof the allocation was made; SUCCESS remove_from_vlan and delete_vlan
runs on cancel are proof the membership was released and the definition undefined
(the allocation itself is still freed in-DB by the ledger teardown).

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
    """A device template wired to the mock L2 driver.

    non-exclusive, mirroring the real seeded L2 switch templates (shared
    infrastructure, seed_devices_public.py): the canvas now names the switch as
    a real endpoint device so every reservation booking it must include it
    (issue #701 phase 2's membership check), and
    test_vlan_ids_are_unique_within_same_fabric below books the SAME switch
    into two overlapping reservations, which a still-exclusive switch would
    409 as a time conflict.
    """
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
            "exclusive": False,
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
    # The cabling connection_type field is irrelevant to L2 membership: the L2 reconcile
    # derives membership from the fork's recorded hops (option C), keying on the far-end
    # device's driver connection_type ("Layer 2 Switch"), not on this field.
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
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-vlan-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _create_reservation(client, device_ids: list[str], topology_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": "vlan assignment integration test",
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


def _vlan_of(run: dict) -> int:
    """Read the HERD-assigned vlan_id from a VLAN membership or definition run.

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
    """Reserving a DUT wired to an L2 switch in the topology drives create_vlan then
    add_to_vlan on the switch with an HERD-assigned vlan_id at activation (ADR 0009
    phase 7: the activation-staged wiring_changed reconcile provisions the initial L2
    membership off the fork, no fork save needed; issue #442: the allocation's first
    built membership defines the VLAN on the switch before the membership joins)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l2_template["id"], f"mock-l2-sw-{suffix}")
    reservation = None
    connection = None
    topology_id = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "eth1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )
        assert await _poll_active(admin_client, reservation["id"]), "reservation never activated"

        add_runs = await _poll_success_runs(admin_client, reservation["id"], "add_to_vlan")
        assert add_runs, "no SUCCESS add_to_vlan run was recorded for the L2 switch"
        run = add_runs[0]
        assert str(run["device_id"]) == switch["id"]
        vlan_id = _vlan_of(run)
        assert 2 <= vlan_id <= 4094, f"vlan_id {vlan_id} out of the valid 2..4094 range"
        # The port joined is the fork hop's switch-side port.
        assert run["input_params"]["method_kwargs"]["port"] == "eth1"
        # Define on allocation (issue #442): the first built membership drove a SUCCESS
        # create_vlan on the switch, naming the same allocated VLAN number.
        create_runs = await _poll_success_runs(admin_client, reservation["id"], "create_vlan")
        assert create_runs, (
            "no SUCCESS create_vlan run was recorded: issue #442 defines the VLAN on "
            "the allocation's first built membership"
        )
        assert str(create_runs[0]["device_id"]) == switch["id"]
        assert _vlan_of(create_runs[0]) == vlan_id, "definition and membership share the VLAN id"
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_vlan_released_on_reservation_cancel(admin_client, l2_template, fresh_device):
    """Cancelling an L2 reservation drives remove_from_vlan then delete_vlan on the
    switch (proof the membership was released and the definition undefined).

    The terminal teardown is ledger-driven (ADR 0009 phase 6): it removes the stored
    l2_port_assignments membership via remove_from_vlan and frees the vlan_assignments
    allocation in the database. As of issue #442 the last-free also undefines the VLAN:
    a SUCCESS delete_vlan run on the switch is the observable definition cleanup."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l2_template["id"], f"mock-l2-sw-{suffix}")
    connection = None
    topology_id = None
    reservation = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "eth1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )
        assert await _poll_active(admin_client, reservation["id"]), "reservation never activated"

        # Provision first (activation-staged reconcile), so there is something to release.
        assert await _poll_success_runs(admin_client, reservation["id"], "add_to_vlan"), (
            "reservation never provisioned the L2 membership, cannot test release"
        )

        resp = await admin_client.delete(f"/reservations/{reservation['id']}")
        assert resp.status_code == 204, resp.text

        remove_runs = await _poll_success_runs(admin_client, reservation["id"], "remove_from_vlan")
        assert remove_runs, "no SUCCESS remove_from_vlan run was recorded after cancel"
        assert str(remove_runs[0]["device_id"]) == switch["id"]
        # Undefine on last-free (issue #442): the freed allocation drives delete_vlan on
        # the switch it was defined on, with the same VLAN number.
        delete_runs = await _poll_success_runs(admin_client, reservation["id"], "delete_vlan")
        assert delete_runs, (
            "no SUCCESS delete_vlan run was recorded after cancel: issue #442 undefines "
            "the VLAN on last-free"
        )
        assert str(delete_runs[0]["device_id"]) == switch["id"]
        assert _vlan_of(delete_runs[0]) == _vlan_of(remove_runs[0])
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
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
    topology_ids = []
    try:
        connections.append(
            await _create_connection(admin_client, dut_a["id"], switch["id"], "eth1")
        )
        connections.append(
            await _create_connection(admin_client, dut_b["id"], switch["id"], "eth2")
        )

        # Each reservation books its own wired topology (its DUT to the shared switch),
        # so activation stages each one's initial L2 membership reconcile independently.
        topo_a = await _create_topology(admin_client, _canvas_edge(dut_a["id"], switch["id"]))
        topology_ids.append(topo_a)
        topo_b = await _create_topology(admin_client, _canvas_edge(dut_b["id"], switch["id"]))
        topology_ids.append(topo_b)

        res_a = await _create_reservation(admin_client, [dut_a["id"], switch["id"]], topo_a)
        reservations.append(res_a)
        res_b = await _create_reservation(admin_client, [dut_b["id"], switch["id"]], topo_b)
        reservations.append(res_b)
        assert await _poll_active(admin_client, res_a["id"]), "reservation A never activated"
        assert await _poll_active(admin_client, res_b["id"]), "reservation B never activated"

        runs_a = await _poll_success_runs(admin_client, res_a["id"], "add_to_vlan")
        runs_b = await _poll_success_runs(admin_client, res_b["id"], "add_to_vlan")
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
        for tid in topology_ids:
            await admin_client.delete(f"/cabling/topologies/{tid}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
