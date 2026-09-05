"""Integration tests for L3 route provisioning via a mock L3 switch driver.

As of ADR 0009 phase 7 all wiring, initial provisioning included, is fork-driven: a
reservation books a WIRED parent topology, activation stages a reservation.wiring_changed
for the fork's initial version, and the execution consumer's layered reconcile derives L3
adjacency from the fork's recorded hops (issue #20 pin lifecycle unchanged: the routes come
from the switch's latest config version at provision time). No fork save is needed;
activation provisions the initial routes directly. These tests upload the checked-in mock
L3 driver (drivers/mock_l3), wire a DUT to a Layer 3 Switch device on both the physical
connection graph and the topology canvas, store a config version whose routes array is the
route source of truth, reserve the DUT with that topology, and assert the resulting
configure_route / remove_route operations on the switch via GET /execution/runs.

There is no REST endpoint for RouteAssignment rows, so we assert the observable
downstream effect instead: the driver actually ran the route ops with the
config's routes. A SUCCESS configure_route run only exists after record_route_active
pinned the set, so it is end-to-end proof the assignment was made; a SUCCESS
remove_route run after cancel is proof it was released. The config-edit test
pins the core invariant: deprovision removes the PINNED set, not the edited
config's routes.

The suite self-seeds the mock L3 driver via a session fixture.
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

_MOCK_L3_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l3"

ROUTES = [
    {"destination": "10.20.0.0/24", "next_hop": "192.168.50.1", "interface": "eth0"},
    {"destination": "10.21.0.0/24", "interface": "eth1"},
]

EDITED_ROUTES = [
    {"destination": "172.30.0.0/16", "next_hop": "192.168.50.254", "interface": "eth2"},
]


def _mock_l3_tarball() -> bytes:
    """Package the checked-in drivers/mock_l3 package into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L3_DIR / name, arcname=name)
    return buf.getvalue()


@pytest.fixture(scope="session")
async def l3_driver(base_url, admin_token):
    """Upload the mock Layer 3 Switch driver once per session."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        files = {"file": ("mock_l3.tar.gz", _mock_l3_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l3-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 3 Switch",
            "description": "integration mock L3 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        assert driver["connection_type"] == "Layer 3 Switch"
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l3_template(base_url, admin_token, l3_driver):
    """A device template wired to the mock L3 driver."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        payload = {
            "name": f"mock-l3-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": l3_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL3Switch",
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
    # The cabling connection_type field is irrelevant to L3 adjacency: the L3 reconcile
    # derives adjacency from the fork's recorded hops, keying on the far-end device's
    # driver connection_type ("Layer 3 Switch"), not on this field.
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
    resp = await client.post("/cabling/topologies", json={"name": f"int-l3-{uuid.uuid4().hex[:8]}"})
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _create_config_version(client, device_id: str, routes: list[dict]) -> dict:
    """Store a config version whose routes array is the provisioning source."""
    resp = await client.post(
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": {"routes": routes}, "description": "l3 integration routes"},
    )
    resp.raise_for_status()
    return resp.json()


async def _create_reservation(client, device_ids: list[str], topology_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": "l3 route provisioning integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


def _route_kwargs_of(run: dict) -> dict:
    """Read the driver method kwargs from a configure_route / remove_route run.

    create_execution_run nests the driver method kwargs under
    input_params["method_kwargs"] for queryability (execution_service.py:44-47).
    """
    return run["input_params"]["method_kwargs"]


def _as_kwarg_set(runs: list[dict]) -> set[tuple]:
    return {
        (k["destination"], k.get("next_hop"), k["interface"])
        for k in (_route_kwargs_of(r) for r in runs)
    }


def _expected_set(routes: list[dict]) -> set[tuple]:
    # The executor passes next_hop as None when the config omits it (the L3
    # schema types next_hop as a string, so an interface route omits the key).
    return {(r["destination"], r.get("next_hop"), r["interface"]) for r in routes}


async def _poll_success_runs(
    client, reservation_id: str, action: str, *, timeout: float = 30.0, interval: float = 0.5
) -> list[dict]:
    """Poll GET /execution/runs until a SUCCESS run with `action` appears.

    Returns the list of matching SUCCESS runs (empty on timeout). Provisioning is
    asynchronous (NATS event to consumer to driver subprocess), so the test waits
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


async def _poll_reservation_status(
    client, reservation_id: str, wanted: str, *, timeout: float = 30.0, interval: float = 0.5
) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == wanted:
            return True
        await asyncio.sleep(interval)
    return False


async def test_routes_configured_on_reservation_create_with_l3_switch(
    admin_client, l3_template, fresh_device
):
    """Reserving a DUT wired to an L3 switch in the topology drives one configure_route
    per route in the switch's latest config version at activation, including the null
    next_hop passthrough for an interface route (ADR 0009 phase 7: the activation-staged
    wiring_changed reconcile provisions the initial routes off the fork)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l3_template["id"], f"mock-l3-sw-{suffix}")
    reservation = None
    connection = None
    topology_id = None
    try:
        await _create_config_version(admin_client, switch["id"], ROUTES)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )

        runs = await _poll_success_runs(admin_client, reservation["id"], "configure_route")
        assert runs, "no SUCCESS configure_route run was recorded for the L3 switch"
        assert {str(r["device_id"]) for r in runs} == {switch["id"]}

        # Poll until both routes have landed (they arrive as separate runs).
        deadline = asyncio.get_event_loop().time() + 30.0
        while len(runs) < len(ROUTES) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            runs = await _poll_success_runs(admin_client, reservation["id"], "configure_route")
        assert _as_kwarg_set(runs) == _expected_set(ROUTES)
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_routes_removed_on_reservation_cancel(admin_client, l3_template, fresh_device):
    """Cancelling an L3 reservation drives one remove_route per pinned route."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l3_template["id"], f"mock-l3-sw-{suffix}")
    connection = None
    topology_id = None
    try:
        await _create_config_version(admin_client, switch["id"], ROUTES)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )

        # Provision first, so there is something to release.
        assert await _poll_success_runs(admin_client, reservation["id"], "configure_route"), (
            "reservation never provisioned routes, cannot test removal"
        )

        resp = await admin_client.delete(f"/reservations/{reservation['id']}")
        assert resp.status_code == 204, resp.text

        remove_runs = await _poll_success_runs(admin_client, reservation["id"], "remove_route")
        assert remove_runs, "no SUCCESS remove_route run was recorded after cancel"
        assert {str(r["device_id"]) for r in remove_runs} == {switch["id"]}
        deadline = asyncio.get_event_loop().time() + 30.0
        while len(remove_runs) < len(ROUTES) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            remove_runs = await _poll_success_runs(admin_client, reservation["id"], "remove_route")
        assert _as_kwarg_set(remove_runs) == _expected_set(ROUTES)
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_route_removal_matches_provisioned_set_after_config_edit(
    admin_client, l3_template, fresh_device
):
    """The core issue #20 invariant, live: a config version written mid-
    reservation does NOT change what deprovision removes; remove_route targets
    exactly the set configure_route applied."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l3_template["id"], f"mock-l3-sw-{suffix}")
    connection = None
    topology_id = None
    try:
        await _create_config_version(admin_client, switch["id"], ROUTES)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )

        assert await _poll_success_runs(admin_client, reservation["id"], "configure_route"), (
            "reservation never provisioned routes, cannot test the pinned-set invariant"
        )

        # Edit the config mid-reservation: latest version now has EDITED_ROUTES.
        await _create_config_version(admin_client, switch["id"], EDITED_ROUTES)

        resp = await admin_client.delete(f"/reservations/{reservation['id']}")
        assert resp.status_code == 204, resp.text

        remove_runs = await _poll_success_runs(admin_client, reservation["id"], "remove_route")
        assert remove_runs, "no SUCCESS remove_route run was recorded after cancel"
        deadline = asyncio.get_event_loop().time() + 30.0
        while len(remove_runs) < len(ROUTES) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            remove_runs = await _poll_success_runs(admin_client, reservation["id"], "remove_route")

        removed = _as_kwarg_set(remove_runs)
        assert removed == _expected_set(ROUTES), (
            f"deprovision must remove the pinned set, got {removed}"
        )
        assert not removed & _expected_set(EDITED_ROUTES), (
            "deprovision must never touch routes from the edited config"
        )
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_no_route_ops_when_l3_device_has_no_config(admin_client, l3_template, fresh_device):
    """An L3 switch with no config version provisions nothing and does not
    block the reservation from activating (the reconcile's empty-config skip)."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(admin_client, l3_template["id"], f"mock-l3-sw-{suffix}")
    reservation = None
    connection = None
    topology_id = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )

        # Deterministic anchor: the reservation still activates.
        assert await _poll_reservation_status(admin_client, reservation["id"], "ACTIVE"), (
            "reservation never became ACTIVE"
        )

        resp = await admin_client.get(
            "/execution/runs",
            params={"reservation_id": reservation["id"], "limit": 200},
        )
        resp.raise_for_status()
        actions = {r["action"] for r in resp.json().get("items", [])}
        assert "configure_route" not in actions, (
            "configure_route must not run for a switch with no config version"
        )
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
