"""Integration coverage for the L3 layered adjacency reconcile (ADR 0009 phase 5, #416).

End-to-end over a running HERD stack with the checked-in mock L3 driver. L3 route
provisioning is now derived from the fork's recorded hops (option C adjacency) and
reconciled on reservation.wiring_changed, with the issue #20 pin lifecycle unchanged
(routes come from the switch's latest config version at first provision, pinned per
reservation, and every later removal/retry drives exactly that pinned set):

- Provision on gained adjacency: activate a DUT cabled to an L3 switch against an empty
  fork, save a canvas that wires them, and assert the switch's pinned routes are
  configured (a configure_route per route). Asserted through GET /execution/runs.
- Shared adjacency keeps routes: a second DUT also cabled to the same switch, then a
  re-wire that keeps one hop on the switch must NOT deprovision (no remove_route).
- Deprovision on lost adjacency: emptying the canvas removes exactly the pinned set
  (a remove_route per route).
- Result gating and manual retry (issue #393 / #369 for L3): a fork-save provision forced
  to fail via HERD_mock_fail_actions=configure_route lands a FAILED route pin, surfaced as
  a layer "l3" outcome through POST /reservations/{id}/wiring/retry; clearing the knob and
  retrying converges it ACTIVE ("reconnected").

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

_MOCK_L3_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l3"

# The routes the L3 switch's config version declares, and therefore the pinned set.
ROUTES = [
    {"destination": "10.10.0.0/24", "next_hop": "192.0.2.1", "interface": "eth0"},
    {"destination": "10.11.0.0/24", "interface": "eth1"},
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


@pytest.fixture(scope="session")
async def l3_driver(base_url, admin_token):
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l3.tar.gz", _tarball(_MOCK_L3_DIR), "application/gzip")}
        data = {
            "name": f"mock-l3-recon-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 3 Switch",
            "description": "L3 reconcile integration mock L3 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l3_switch_template(base_url, admin_token, l3_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l3-recon-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": l3_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL3SwitchReconcile",
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
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


async def _create_switch(client, template_id: str, field_data: dict) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": f"mock-l3-sw-{uuid.uuid4().hex[:8]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": field_data,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _set_switch_config(client, switch_id: str) -> None:
    """Publish a config version declaring the routes the pin will capture (issue #20)."""
    resp = await client.post(
        f"/inventory/devices/{switch_id}/config-versions",
        json={"config": {"routes": ROUTES}, "description": "L3 reconcile integration routes"},
    )
    resp.raise_for_status()


async def _connect(client, dut_id: str, dut_port: str, switch_id: str, switch_port: str) -> dict:
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


async def _reserve(client, device_ids: list[str], topology_id: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "L3 reconcile integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if topology_id is not None:
        body["topology_id"] = topology_id
    resp = await client.post("/reservations/", json=body)
    resp.raise_for_status()
    return resp.json()


async def _runs(client, reservation_id: str, action: str, status: str = "SUCCESS") -> list[dict]:
    resp = await client.get(
        "/execution/runs",
        params={"reservation_id": reservation_id, "status": status, "limit": 300},
    )
    if resp.status_code != 200:
        return []
    return [r for r in resp.json().get("items", []) if r["action"] == action]


async def _poll_route_run(client, reservation_id, action, destination, *, timeout=25.0):
    """Poll for a run of `action` for route `destination` (port_a), returning it or None.

    The L3 route run identity packs destination into port_a (see _route_run_identity).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for r in await _runs(client, reservation_id, action):
            if r.get("port_a") == destination:
                return r
        await asyncio.sleep(0.5)
    return None


async def _no_route_run(client, reservation_id, action, *, window=6.0) -> bool:
    """Return True if NO run of `action` appears within `window` seconds (absence check)."""
    deadline = asyncio.get_event_loop().time() + window
    while asyncio.get_event_loop().time() < deadline:
        if await _runs(client, reservation_id, action):
            return False
        await asyncio.sleep(0.5)
    return True


async def _poll_active(client, reservation_id: str, *, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _poll_retry_l3(client, reservation_id, outcome, *, timeout=25.0):
    """Poll POST wiring/retry until a layer-l3 outcome matches `outcome`."""
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.post(f"/reservations/{reservation_id}/wiring/retry")
        if resp.status_code == 200:
            for row in resp.json().get("results", []):
                if row.get("layer") == "l3":
                    last = row
                    if row.get("outcome") == outcome:
                        return row
        await asyncio.sleep(0.5)
    return last


async def _save_fork(client, reservation_id, canvas):
    return await client.post(
        f"/reservations/{reservation_id}/fork/save", json={"canvas_data": canvas}
    )


async def test_l3_routes_provision_on_gained_adjacency_and_release_on_lost(
    base_url, admin_token, admin_client, fresh_devices, l3_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    (dut,) = await fresh_devices(1)
    switch = await _create_switch(admin_client, l3_switch_template["id"], {"model": "test"})
    await _set_switch_config(admin_client, switch["id"])
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(
            await _connect(admin_client, dut["id"], "eth0", switch["id"], "ge-0/0/1")
        )
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        # Save the wired canvas: the reconcile provisions the switch's pinned routes.
        saved = await _save_fork(admin_client, rid, _canvas_edge(dut["id"], switch["id"]))
        assert saved.status_code == 200, saved.text
        assert await _poll_route_run(admin_client, rid, "configure_route", "10.10.0.0/24"), (
            "route 10.10.0.0/24 was never configured"
        )
        assert await _poll_route_run(admin_client, rid, "configure_route", "10.11.0.0/24"), (
            "route 10.11.0.0/24 was never configured"
        )

        # Save an emptied canvas: the full reconcile removes exactly the pinned set.
        emptied = await _save_fork(admin_client, rid, {"nodes": [], "edges": []})
        assert emptied.status_code == 200, emptied.text
        assert await _poll_route_run(admin_client, rid, "remove_route", "10.10.0.0/24"), (
            "route 10.10.0.0/24 was never removed"
        )
        assert await _poll_route_run(admin_client, rid, "remove_route", "10.11.0.0/24"), (
            "route 10.11.0.0/24 was never removed"
        )
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l3_shared_adjacency_keeps_routes_until_last_hop_leaves(
    base_url, admin_token, admin_client, fresh_devices, l3_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    dut_a, dut_b = await fresh_devices(2)
    switch = await _create_switch(admin_client, l3_switch_template["id"], {"model": "test"})
    await _set_switch_config(admin_client, switch["id"])
    connections = []
    rid = None
    topology_id = None
    try:
        conn_a = await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "ge-0/0/1")
        conn_b = await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "ge-0/0/2")
        connections.extend([conn_a, conn_b])
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut_a["id"], dut_b["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        # Wire BOTH DUTs to the switch: it provisions once.
        both = {
            "nodes": [
                {"id": "nA", "data": {"device": {"id": dut_a["id"]}}},
                {"id": "nS", "data": {"device": {"id": switch["id"]}}},
                {"id": "nB", "data": {"device": {"id": dut_b["id"]}}},
            ],
            "edges": [
                {"id": "eA", "source": "nA", "target": "nS", "data": {"layer": "L1"}},
                {"id": "eB", "source": "nB", "target": "nS", "data": {"layer": "L1"}},
            ],
        }
        saved = await _save_fork(admin_client, rid, both)
        assert saved.status_code == 200, saved.text
        assert await _poll_route_run(admin_client, rid, "configure_route", "10.10.0.0/24")

        # Re-wire keeping only DUT-a on the switch: the switch is still adjacent, so NO
        # deprovision fires (shared derived adjacency; only the intended SET can end it).
        only_a = {
            "nodes": [
                {"id": "nA", "data": {"device": {"id": dut_a["id"]}}},
                {"id": "nS", "data": {"device": {"id": switch["id"]}}},
            ],
            "edges": [{"id": "eA", "source": "nA", "target": "nS", "data": {"layer": "L1"}}],
        }
        resaved = await _save_fork(admin_client, rid, only_a)
        assert resaved.status_code == 200, resaved.text
        assert await _no_route_run(admin_client, rid, "remove_route"), (
            "a still-adjacent L3 switch must NOT be deprovisioned"
        )
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l3_failed_provision_surfaces_and_manual_retry_recovers(
    base_url, admin_token, admin_client, fresh_devices, l3_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    (dut,) = await fresh_devices(1)
    switch = await _create_switch(
        admin_client,
        l3_switch_template["id"],
        {"model": "test", "mock_fail_actions": "configure_route"},
    )
    await _set_switch_config(admin_client, switch["id"])
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(
            await _connect(admin_client, dut["id"], "eth0", switch["id"], "ge-0/0/1")
        )
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        saved = await _save_fork(admin_client, rid, _canvas_edge(dut["id"], switch["id"]))
        assert saved.status_code == 200, saved.text

        # The failed provision surfaces as a layer-l3 FAILED outcome (still_failed while
        # the knob is armed).
        failed = await _poll_retry_l3(admin_client, rid, "still_failed")
        assert failed is not None and failed["layer"] == "l3", "no FAILED L3 pin surfaced"
        assert failed["route_count"] == len(ROUTES)

        # Clear the knob and retry: the provision converges ACTIVE.
        cleared = await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": ""}},
        )
        assert cleared.status_code == 200, cleared.text
        recovered = await _poll_retry_l3(admin_client, rid, "reconnected")
        assert recovered is not None, "the route pin never converged ACTIVE after retry"
        assert recovered["outcome"] == "reconnected"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
