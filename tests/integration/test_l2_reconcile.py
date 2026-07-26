"""Integration coverage for the L2 layered membership reconcile (ADR 0009 phase 4, #416).

End-to-end over a running HERD stack with the checked-in mock L2 driver. L2 membership
is now derived from the fork's recorded hops (option C) and reconciled on
reservation.wiring_changed, split from the per-fabric VLAN-number allocation:

- Provision then release: activate two DUTs cabled through an L2 switch against an empty
  fork, save a canvas that wires them (each DUT's switch-side port joins the VLAN, an
  add_to_vlan per port), then save an emptied canvas (both ports leave, a remove_from_vlan
  per port). Asserted through GET /execution/runs, the observable proxy for the
  l2_port_assignments flip (the L2 wiring-status surface is ADR 0009 phase 8).
- Move a port (ADR 0007 open risk 5, the case this phase closes): re-wire a DUT to a
  different switch port and assert the old port leaves and the new joins on the SAME VLAN
  (the fabric keeps its allocation across a port move). The same-VLAN invariant is also
  pinned at the unit level (test_nats_consumer_l2_reconcile.test_reconcile_move_a_port_*).
- Result gating and manual retry (issue #393 / #369 for L2): a fork-save join forced to
  fail via HERD_mock_fail_actions=add_to_vlan lands a FAILED membership row, surfaced as a
  layer "l2" outcome through POST /reservations/{id}/wiring/retry; clearing the knob and
  retrying converges it ACTIVE ("reconnected").
- Release-direction retry: a fork-save leave forced to fail via
  HERD_mock_fail_actions=remove_from_vlan lands a FAILED row intended RELEASED; clearing
  the knob and retrying converges it RELEASED ("released").

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

_MOCK_L2_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l2"


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
async def l2_driver(base_url, admin_token):
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l2.tar.gz", _tarball(_MOCK_L2_DIR), "application/gzip")}
        data = {
            "name": f"mock-l2-recon-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 2 Switch",
            "description": "L2 reconcile integration mock L2 switch driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l2_switch_template(base_url, admin_token, l2_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l2-recon-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": l2_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL2SwitchReconcile",
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
            "name": f"mock-l2-sw-{uuid.uuid4().hex[:8]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": field_data,
        },
    )
    resp.raise_for_status()
    return resp.json()


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
    resp = await client.post("/cabling/topologies", json={"name": f"int-l2-{uuid.uuid4().hex[:8]}"})
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_ids: list[str], topology_id: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "L2 reconcile integration test",
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


async def _poll_run(client, reservation_id, action, port, *, status="SUCCESS", timeout=25.0):
    """Poll for a run of `action` on switch `port` (port_a), returning it or None."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for r in await _runs(client, reservation_id, action, status):
            if r.get("port_a") == port:
                return r
        await asyncio.sleep(0.5)
    return None


async def _poll_active(client, reservation_id: str, *, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _poll_retry_l2(client, reservation_id, port, outcome, *, timeout=25.0):
    """Poll POST wiring/retry until a layer-l2 outcome for `port` matches `outcome`."""
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


async def _save_fork(client, reservation_id, canvas):
    return await client.post(
        f"/reservations/{reservation_id}/fork/save", json={"canvas_data": canvas}
    )


async def test_l2_membership_provisions_then_releases_on_fork_save(
    base_url, admin_token, admin_client, fresh_devices, l2_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    dut_a, dut_b = await fresh_devices(2)
    switch = await _create_switch(admin_client, l2_switch_template["id"], {"model": "test"})
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "1"))
        connections.append(await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "2"))
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut_a["id"], dut_b["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        # Save the wired canvas: the reconcile joins each DUT's switch port to the VLAN.
        saved = await _save_fork(admin_client, rid, _canvas_edge(dut_a["id"], dut_b["id"]))
        assert saved.status_code == 200, saved.text
        assert await _poll_run(admin_client, rid, "add_to_vlan", "1"), "port 1 never joined"
        assert await _poll_run(admin_client, rid, "add_to_vlan", "2"), "port 2 never joined"

        # Save an emptied canvas: the full reconcile leaves both ports.
        emptied = await _save_fork(admin_client, rid, {"nodes": [], "edges": []})
        assert emptied.status_code == 200, emptied.text
        assert await _poll_run(admin_client, rid, "remove_from_vlan", "1"), "port 1 never left"
        assert await _poll_run(admin_client, rid, "remove_from_vlan", "2"), "port 2 never left"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l2_membership_moves_to_new_port_same_vlan(
    base_url, admin_token, admin_client, fresh_devices, l2_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    dut_a, dut_b = await fresh_devices(2)
    switch = await _create_switch(admin_client, l2_switch_template["id"], {"model": "test"})
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "1"))
        conn_b = await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "2")
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut_a["id"], dut_b["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        saved = await _save_fork(admin_client, rid, _canvas_edge(dut_a["id"], dut_b["id"]))
        assert saved.status_code == 200, saved.text
        add2 = await _poll_run(admin_client, rid, "add_to_vlan", "2")
        assert add2 is not None, "port 2 never joined"
        vlan_before = ((add2.get("input_params") or {}).get("method_kwargs") or {}).get("vlan_id")

        # Move DUT-b from switch port 2 to port 3, then re-save: the reconcile removes the
        # old port and adds the new one on the same fabric VLAN.
        await admin_client.delete(f"/cabling/connections/{conn_b['id']}")
        connections.append(await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "3"))
        resaved = await _save_fork(admin_client, rid, _canvas_edge(dut_a["id"], dut_b["id"]))
        assert resaved.status_code == 200, resaved.text

        assert await _poll_run(admin_client, rid, "remove_from_vlan", "2"), "old port never left"
        add3 = await _poll_run(admin_client, rid, "add_to_vlan", "3")
        assert add3 is not None, "new port never joined"
        vlan_after = ((add3.get("input_params") or {}).get("method_kwargs") or {}).get("vlan_id")
        assert vlan_before is not None and vlan_after == vlan_before, "membership kept the VLAN"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l2_failed_add_surfaces_and_manual_retry_recovers(
    base_url, admin_token, admin_client, fresh_devices, l2_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    dut_a, dut_b = await fresh_devices(2)
    switch = await _create_switch(
        admin_client,
        l2_switch_template["id"],
        {"model": "test", "mock_fail_actions": "add_to_vlan"},
    )
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "1"))
        connections.append(await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "2"))
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut_a["id"], dut_b["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        saved = await _save_fork(admin_client, rid, _canvas_edge(dut_a["id"], dut_b["id"]))
        assert saved.status_code == 200, saved.text

        # The failed join surfaces as a layer-l2 FAILED outcome (still_failed on retry
        # while the knob is armed).
        failed = await _poll_retry_l2(admin_client, rid, "1", "still_failed")
        assert failed is not None and failed["layer"] == "l2", "no FAILED L2 membership surfaced"

        # Clear the knob and retry: the join converges ACTIVE.
        cleared = await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": ""}},
        )
        assert cleared.status_code == 200, cleared.text
        recovered = await _poll_retry_l2(admin_client, rid, "1", "reconnected")
        assert recovered is not None, "the membership never converged ACTIVE after retry"
        assert recovered["outcome"] == "reconnected"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l2_release_failure_retry_converges_released(
    base_url, admin_token, admin_client, fresh_devices, l2_switch_template
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    dut_a, dut_b = await fresh_devices(2)
    switch = await _create_switch(admin_client, l2_switch_template["id"], {"model": "test"})
    connections = []
    rid = None
    topology_id = None
    try:
        connections.append(await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "1"))
        connections.append(await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "2"))
        topology_id = await _create_topology(admin_client, {"nodes": [], "edges": []})
        res = await _reserve(admin_client, [dut_a["id"], dut_b["id"], switch["id"]], topology_id)
        rid = res["id"]
        assert await _poll_active(admin_client, rid), "reservation never activated"

        saved = await _save_fork(admin_client, rid, _canvas_edge(dut_a["id"], dut_b["id"]))
        assert saved.status_code == 200, saved.text
        assert await _poll_run(admin_client, rid, "add_to_vlan", "1"), "port 1 never joined"

        # Arm the remove knob, then save an emptied canvas so the leave is forced to fail.
        await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": "remove_from_vlan"}},
        )
        emptied = await _save_fork(admin_client, rid, {"nodes": [], "edges": []})
        assert emptied.status_code == 200, emptied.text

        failed = await _poll_retry_l2(admin_client, rid, "1", "still_failed")
        assert failed is not None and failed["layer"] == "l2", "no FAILED release membership"

        # Clear the knob and retry: the leave converges RELEASED.
        await admin_client.put(
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": ""}},
        )
        released = await _poll_retry_l2(admin_client, rid, "1", "released")
        assert released is not None, "the membership never converged RELEASED after retry"
        assert released["outcome"] == "released"
    finally:
        if rid:
            await admin_client.delete(f"/reservations/{rid}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
