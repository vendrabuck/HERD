"""Integration tests for reservation.failed provisioning teardown (issue #244).

The invariant under test: reservation.failed tears down exactly the provisioning
that landed before the failure, and nothing else. As of ADR 0009 phase 6 the
teardown is ledger-driven (an ACTIVE ledger row IS an applied op), and as of
phase 7 the provisioning that writes those ledgers is fork-driven: each scenario
books a reservation against a WIRED parent topology, so the activation-staged
reservation.wiring_changed reconcile provisions the initial wiring and records
the ledger rows the teardown then releases.

The failed events themselves are still published synthetically onto the
reservations stream: the consumer acts on its own ledgers and validates nothing
against the reservations service, which is exactly the contract issue #244
hardens (the teardown invariant must hold for any producer ordering, current or
future). Observability is GET /execution/runs (there is no REST endpoint for
vlan/route assignment rows; a SUCCESS remove_* run driven by the stored state is
the end-to-end proof the stored state existed and was used).

Ordering anchor pattern: the execution consumer processes the reservations
stream sequentially, so publishing a later event and waiting for its runs
proves an earlier event was fully processed; that turns "assert nothing
happened" into a deterministic check instead of a sleep.
"""

import asyncio
import io
import json
import os
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import nats
import pytest

pytestmark = pytest.mark.asyncio

NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
_DRIVERS_DIR = Path(__file__).resolve().parents[2] / "drivers"

ROUTES = [
    {"destination": "10.44.0.0/24", "next_hop": "192.168.44.1", "interface": "eth0"},
    {"destination": "10.45.0.0/24", "interface": "eth1"},
]


def _mock_tarball(package: str) -> bytes:
    """Package a checked-in drivers/<package> into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_DRIVERS_DIR / package / name, arcname=name)
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


async def _publish_event(event: str, reservation_id: str, device_ids: list[str]) -> dict:
    """Publish a synthetic reservation lifecycle event to the reservations stream.

    Stamps a fresh event_id (as the outbox relay does); publishing the SAME
    returned payload again simulates a JetStream redelivery / relay republish of
    one logical event.
    """
    payload = {
        "event": event,
        "event_id": str(uuid.uuid4()),
        "reservation_id": reservation_id,
        "user_id": str(uuid.uuid4()),
        "device_ids": device_ids,
    }
    await _publish_raw(f"herd.reservations.{event.split('.')[1]}", json.dumps(payload).encode())
    return payload


async def _publish_raw(subject: str, payload: bytes) -> None:
    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        # Confirm the stream exists rather than re-declaring it: the stream is
        # created by the reservations/execution lifespan, and add_stream
        # against an existing stream with a different config (e.g. a
        # configured max_age, issue #620) raises instead of returning it.
        await js.stream_info("HERD_RESERVATIONS")
        await js.publish(subject, payload)
    finally:
        await nc.close()


async def _runs(client, reservation_id: str, action: str | None = None, status: str | None = None):
    params = {"reservation_id": reservation_id, "limit": 200}
    if status:
        params["status"] = status
    resp = await client.get("/execution/runs", params=params)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if action:
        items = [r for r in items if r["action"] == action]
    return items


async def _poll_runs(
    client,
    reservation_id: str,
    action: str,
    *,
    status: str = "SUCCESS",
    count: int = 1,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> list[dict]:
    """Poll GET /execution/runs until `count` runs of (action, status) appear.

    Returns the matching runs (possibly short on timeout; callers assert).
    Provisioning is asynchronous (NATS event to consumer to driver subprocess),
    so tests wait rather than asserting immediately after the publish.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    matched: list[dict] = []
    while asyncio.get_event_loop().time() < deadline:
        matched = await _runs(client, reservation_id, action, status)
        if len(matched) >= count:
            return matched
        await asyncio.sleep(interval)
    return matched


def _method_kwargs(run: dict) -> dict:
    return run["input_params"]["method_kwargs"]


@pytest.fixture(scope="session")
async def teardown_drivers(base_url, admin_token):
    """Upload the three mock switch drivers once per session."""
    uploaded = {}
    async with _admin_session_client(base_url, admin_token) as client:
        for package, ctype in (
            ("mock_l1", "Layer 1 Switch"),
            ("mock_l2", "Layer 2 Switch"),
            ("mock_l3", "Layer 3 Switch"),
        ):
            files = {"file": (f"{package}.tar.gz", _mock_tarball(package), "application/gzip")}
            data = {
                "name": f"{package}-failed-{uuid.uuid4().hex[:8]}",
                "connection_type": ctype,
                "description": f"issue #244 failed-teardown {package} driver",
            }
            resp = await client.post("/inventory/drivers", files=files, data=data)
            resp.raise_for_status()
            uploaded[package] = resp.json()
        yield uploaded
        for driver in uploaded.values():
            await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def teardown_templates(base_url, admin_token, teardown_drivers):
    """One template per mock driver; each declares the failure-injection field
    so a switch device can carry per-action knobs in its field_data."""
    templates = {}
    async with _admin_session_client(base_url, admin_token) as client:
        for package, driver in teardown_drivers.items():
            payload = {
                "name": f"{package}-failed-tmpl-{uuid.uuid4().hex[:8]}",
                "template_type": "device",
                "driver_id": driver["id"],
                # mock_l3 is non-exclusive: the L3 redelivery test below books
                # the SAME switch as a canvas endpoint device into two
                # overlapping reservations (res_a, res_b), which a still-
                # exclusive switch would 409 as a time conflict (issue #701
                # phase 2's membership check now requires every canvas
                # endpoint device to be booked).
                "exclusive": package != "mock_l3",
                "vendor": "IntegrationVendor",
                "model": f"FailedTeardown-{package}",
                "sections": [
                    {
                        "name": "General",
                        "fields": [
                            {"key": "model", "label": "Model", "type": "string"},
                            {
                                "key": "mock_raise_actions",
                                "label": "Mock raise actions",
                                "type": "string",
                            },
                        ],
                    }
                ],
            }
            resp = await client.post("/inventory/templates", json=payload)
            resp.raise_for_status()
            templates[package] = resp.json()
        yield templates
        for template in templates.values():
            await client.delete(f"/inventory/templates/{template['id']}")


async def _create_switch(client, template_id: str, name: str, raise_actions: str = "") -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "sw", "mock_raise_actions": raise_actions},
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


async def _create_config_version(client, device_id: str, routes: list[dict]) -> dict:
    resp = await client.post(
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": {"routes": routes}, "description": "issue #244 teardown routes"},
    )
    resp.raise_for_status()
    return resp.json()


def _canvas(edges: list[tuple[str, str]]) -> dict:
    """A committed canvas wiring each (a_id, b_id) pair (React Flow shape)."""
    node_ids: dict[str, str] = {}
    nodes = []
    for a_id, b_id in edges:
        for device_id in (a_id, b_id):
            if device_id not in node_ids:
                node_ids[device_id] = f"n{len(node_ids)}"
                nodes.append({"id": node_ids[device_id], "data": {"device": {"id": device_id}}})
    return {
        "nodes": nodes,
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
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-244-{uuid.uuid4().hex[:8]}"}
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
            "topology_id": topology_id,
            "purpose": "issue #244 failed-teardown test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def test_l3_failed_event_removes_pinned_routes_and_redelivery_is_idempotent(
    admin_client, teardown_templates, fresh_devices
):
    """reservation.failed after full L3 provisioning removes exactly the pinned
    routes; a redelivered failed event (same event_id) tears down nothing more."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_switch(
        admin_client, teardown_templates["mock_l3"]["id"], f"failed-l3-sw-{suffix}"
    )
    dut_a, dut_b = await fresh_devices(2)
    connections = []
    topology_ids = []
    reservations = []
    try:
        await _create_config_version(admin_client, switch["id"], ROUTES)
        connections.append(
            await _connect(admin_client, dut_a["id"], "eth0", switch["id"], "ge-0/0/1")
        )

        # Provision through the phase-7 path: a wired reservation whose activation
        # stages the wiring_changed reconcile that pins and configures the routes.
        topo_a = await _create_topology(admin_client, _canvas([(dut_a["id"], switch["id"])]))
        topology_ids.append(topo_a)
        res_a = await _reserve(admin_client, [dut_a["id"], switch["id"]], topo_a)
        reservations.append(res_a)
        res_id = res_a["id"]
        assert len(
            await _poll_runs(admin_client, res_id, "configure_route", count=len(ROUTES))
        ) == len(ROUTES), "provisioning never completed, cannot test failed teardown"

        failed_payload = await _publish_event("reservation.failed", res_id, [dut_a["id"]])
        remove_runs = await _poll_runs(admin_client, res_id, "remove_route", count=len(ROUTES))
        assert len(remove_runs) == len(ROUTES), (
            "reservation.failed did not remove the pinned routes"
        )
        removed = {
            (k["destination"], k.get("next_hop"), k["interface"])
            for k in (_method_kwargs(r) for r in remove_runs)
        }
        assert removed == {(r["destination"], r.get("next_hop"), r["interface"]) for r in ROUTES}

        # Redelivery: the same logical event again (same event_id). The pinned
        # set is RELEASED, so the ledger teardown finds nothing applied and no
        # further teardown may run. Anchor on a later provisioning event (a second
        # wired reservation on the same switch) to avoid a sleep.
        await _publish_raw("herd.reservations.failed", json.dumps(failed_payload).encode())
        connections.append(
            await _connect(admin_client, dut_b["id"], "eth0", switch["id"], "ge-0/0/2")
        )
        topo_b = await _create_topology(admin_client, _canvas([(dut_b["id"], switch["id"])]))
        topology_ids.append(topo_b)
        res_b = await _reserve(admin_client, [dut_b["id"], switch["id"]], topo_b)
        reservations.append(res_b)
        assert await _poll_runs(admin_client, res_b["id"], "configure_route"), (
            "anchor reservation was never provisioned"
        )
        assert len(await _runs(admin_client, res_id, "remove_route", "SUCCESS")) == len(ROUTES), (
            "a redelivered reservation.failed re-ran teardown"
        )
    finally:
        for res in reservations:
            await admin_client.delete(f"/reservations/{res['id']}")
        for topology_id in topology_ids:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection in connections:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_l1_failed_event_disconnects_only_applied_pairs(
    admin_client, teardown_templates, fresh_devices
):
    """Half-provisioned L1: the switch whose connect_ports succeeded is torn
    down; the switch whose connect_ports FAILED gets no driver call at all."""
    suffix = uuid.uuid4().hex[:8]
    template_id = teardown_templates["mock_l1"]["id"]
    sw_ok = await _create_switch(admin_client, template_id, f"failed-l1-ok-{suffix}")
    # mock_raise_actions (not mock_fail_actions): the sandbox keys run status
    # on the driver subprocess exit code, so only a RAISING action records the
    # FAILED run that makes this switch's provisioning genuinely un-applied.
    sw_broken = await _create_switch(
        admin_client, template_id, f"failed-l1-broken-{suffix}", raise_actions="connect_ports"
    )
    duts = await fresh_devices(4)
    connections = []
    topology_id = None
    reservation = None
    device_ids = [d["id"] for d in duts]
    try:
        # One DUT pair cabled through each switch, and one canvas edge per pair, so
        # the fork's hops resolve deterministically: (dut0, dut1) through sw_ok and
        # (dut2, dut3) through sw_broken. (A single DUT pair cabled to BOTH switches
        # would leave the pathfinder free to pick either, making which switch
        # provisions nondeterministic.)
        for i, dut in enumerate(duts[:2], start=1):
            connections.append(
                await _connect(admin_client, dut["id"], "eth0", sw_ok["id"], f"ok-p{i}")
            )
        for i, dut in enumerate(duts[2:], start=1):
            connections.append(
                await _connect(admin_client, dut["id"], "eth0", sw_broken["id"], f"bad-p{i}")
            )
        topology_id = await _create_topology(
            admin_client,
            _canvas([(duts[0]["id"], duts[1]["id"]), (duts[2]["id"], duts[3]["id"])]),
        )
        reservation = await _reserve(admin_client, device_ids, topology_id)
        res_id = reservation["id"]

        ok_connects = await _poll_runs(admin_client, res_id, "connect_ports")
        assert ok_connects, "connect_ports never succeeded on the healthy switch"
        assert {str(r["device_id"]) for r in ok_connects} == {sw_ok["id"]}
        failed_connects = await _poll_runs(admin_client, res_id, "connect_ports", status="FAILED")
        assert failed_connects, "the broken switch was supposed to raise on connect_ports"
        assert {str(r["device_id"]) for r in failed_connects} == {sw_broken["id"]}

        await _publish_event("reservation.failed", res_id, device_ids)
        disconnects = await _poll_runs(admin_client, res_id, "disconnect_ports")
        assert disconnects, "reservation.failed did not disconnect the applied pair"
        assert {str(r["device_id"]) for r in disconnects} == {sw_ok["id"]}, (
            "teardown must only touch the switch whose connect_ports succeeded"
        )
        applied = {(k["port_a"], k["port_b"]) for k in (_method_kwargs(r) for r in ok_connects)}
        torn_down = {(k["port_a"], k["port_b"]) for k in (_method_kwargs(r) for r in disconnects)}
        assert torn_down == applied

        # The broken switch got no disconnect attempt in ANY status: an ACTIVE
        # ledger row is the teardown's only trigger, and its connect never landed
        # one (the applied-state-only guarantee, now intrinsic to the ledger).
        all_runs = await _runs(admin_client, res_id)
        broken_actions = {r["action"] for r in all_runs if str(r["device_id"]) == sw_broken["id"]}
        assert "disconnect_ports" not in broken_actions
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for connection in connections:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{sw_ok['id']}")
        await admin_client.delete(f"/inventory/devices/{sw_broken['id']}")


async def test_failed_event_without_provisioning_tears_down_nothing_then_l2_teardown_works(
    admin_client, teardown_templates, fresh_device
):
    """Phase 1: a reservation.failed with NO prior provisioning drives zero
    driver runs (in particular no derived-VLAN fallback teardown). Phase 2: the
    same topology provisioned then failed removes the STORED VLAN membership.

    The teardown is ledger-driven (ADR 0009 phase 6): it reads the
    l2_port_assignments membership rows (written by the fork-driven reconcile,
    phase 7) and drives remove_from_vlan per port, then frees the
    vlan_assignments allocation IN THE DATABASE (the phase-4 allocation lifecycle
    coupling). As of issue #442 the last-free also undefines the VLAN on the
    switch: the observable release is the remove_from_vlan run carrying the
    STORED VLAN id, followed by a delete_vlan run with the same id."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_switch(
        admin_client, teardown_templates["mock_l2"]["id"], f"failed-l2-sw-{suffix}"
    )
    connection = None
    topology_id = None
    reservation = None
    unprovisioned_res_id = str(uuid.uuid4())
    try:
        connection = await _connect(
            admin_client, fresh_device["id"], "eth0", switch["id"], "ge-0/0/1"
        )

        # Phase 1: failed with nothing applied; the provisioning of the next (real,
        # wired) reservation is the ordering anchor proving it was fully processed.
        await _publish_event("reservation.failed", unprovisioned_res_id, [fresh_device["id"]])
        topology_id = await _create_topology(
            admin_client, _canvas([(fresh_device["id"], switch["id"])])
        )
        reservation = await _reserve(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )
        provisioned_res_id = reservation["id"]
        add_runs = await _poll_runs(admin_client, provisioned_res_id, "add_to_vlan")
        assert add_runs, "anchor provisioning never completed"
        assert await _runs(admin_client, unprovisioned_res_id) == [], (
            "a never-provisioned FAILED reservation must not produce any driver run"
        )

        # Phase 2: fail the provisioned reservation; the ledger-driven teardown removes the
        # stored membership (remove_from_vlan with the stored VLAN id), frees the
        # allocation in the DB, and undefines the VLAN via delete_vlan (issue #442).
        await _publish_event("reservation.failed", provisioned_res_id, [fresh_device["id"]])
        remove_runs = await _poll_runs(admin_client, provisioned_res_id, "remove_from_vlan")
        assert remove_runs, "reservation.failed did not remove the stored VLAN membership"
        provisioned_vlan = _method_kwargs(add_runs[0])["vlan_id"]
        torn_down_vlans = {_method_kwargs(r)["vlan_id"] for r in remove_runs}
        assert torn_down_vlans == {provisioned_vlan}, (
            "teardown must drive the stored VLAN id, not a re-derived one"
        )
        # Undefine on last-free (issue #442): the freed allocation drives delete_vlan
        # with the same stored VLAN id, on the same switch.
        delete_runs = await _poll_runs(admin_client, provisioned_res_id, "delete_vlan")
        assert delete_runs, (
            "no SUCCESS delete_vlan run after teardown: issue #442 undefines on last-free"
        )
        assert {_method_kwargs(r)["vlan_id"] for r in delete_runs} == {provisioned_vlan}
        assert str(delete_runs[0]["device_id"]) == switch["id"]
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
