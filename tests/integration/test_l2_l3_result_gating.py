"""Live coverage for issue #393: L2/L3 provisioning gates on the driver RESULT.

The five L2/L3 driver-call sites in nats_consumer.py (create_vlan, add_to_vlan,
remove_from_vlan, delete_vlan, configure_route/remove_route) previously judged
success from the sandbox's raw transport flag alone. A driver method that
RETURNS {"success": False, ...} without raising (the mock drivers'
HERD_mock_fail_actions convention) still landed the run at SUCCESS. This is the
L2/L3 analogue of test_execution_result_gating.py's #370 coverage for
run_driver_action, and of the L1 conversion (PRs #365/#367) for the connection-
driven wiring path: after the fix these run at FAILED with the driver's error.

For L3 specifically, a driver-result failure on remove_route must also leave
the reservation's route pin ACTIVE (pinned directly against the ledger at the
unit level in test_nats_consumer_l3_reconcile.py and
test_nats_consumer_ledger_teardown.py). There is no REST endpoint for
route_assignments rows, so this suite proves the pin-kept invariant the same
way test_l3_route_provisioning.py proves everything else: through the
execution_runs the driver actually ran, plus the reservation reaching
CANCELLED normally (a driver-result failure ACKs, it never NAKs the message).

As of ADR 0009 phase 7 initial provisioning is fork-driven: each test books a
WIRED parent topology (a committed DUT-to-switch canvas edge), so activation
stages the reservation.wiring_changed reconcile that drives the knobbed
L2/L3 driver calls; no fork save is needed.

Self-seeds its own drivers/templates (session fixtures), per the integration-
test seeding convention.
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
_MOCK_L3_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l3"


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


# --- session-scoped driver/template fixtures (mock_fail_actions-capable) ----


@pytest.fixture(scope="session")
async def gating_l2_driver(base_url, admin_token):
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l2.tar.gz", _tarball(_MOCK_L2_DIR), "application/gzip")}
        data = {
            "name": f"mock-l2-393-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 2 Switch",
            "description": "integration mock L2 driver (issue #393 result gating)",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def gating_l2_template(base_url, admin_token, gating_l2_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l2-393-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": gating_l2_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL2SwitchGating393",
            "sections": [
                {
                    "name": "General",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string"},
                        {
                            "key": "mock_fail_actions",
                            "label": "Mock fail actions",
                            "type": "string",
                        },
                    ],
                }
            ],
        }
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def gating_l3_driver(base_url, admin_token):
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l3.tar.gz", _tarball(_MOCK_L3_DIR), "application/gzip")}
        data = {
            "name": f"mock-l3-393-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 3 Switch",
            "description": "integration mock L3 driver (issue #393 result gating)",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def gating_l3_template(base_url, admin_token, gating_l3_driver):
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l3-393-tmpl-{uuid.uuid4().hex[:8]}",
            "template_type": "device",
            "driver_id": gating_l3_driver["id"],
            "vendor": "IntegrationVendor",
            "model": "MockL3SwitchGating393",
            "sections": [
                {
                    "name": "General",
                    "fields": [
                        {"key": "model", "label": "Model", "type": "string"},
                        {
                            "key": "mock_fail_actions",
                            "label": "Mock fail actions",
                            "type": "string",
                        },
                    ],
                }
            ],
        }
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


# --- helpers -----------------------------------------------------------------


async def _create_device(client, template_id: str, name: str, mock_fail_actions: str) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test", "mock_fail_actions": mock_fail_actions},
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _create_connection(client, dut_id: str, switch_id: str, switch_port: str) -> dict:
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


async def _create_config_version(client, device_id: str, routes: list[dict]) -> dict:
    resp = await client.post(
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": {"routes": routes}, "description": "l3 result-gating routes"},
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
        "/cabling/topologies", json={"name": f"int-393-{uuid.uuid4().hex[:8]}"}
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
            "purpose": "L2/L3 result gating integration test (#393)",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _poll_runs(
    client,
    reservation_id: str,
    action: str,
    *,
    status: str | None = None,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> list[dict]:
    """Poll GET /execution/runs until a run matching `action` (and `status`, if
    given) appears. Returns the matching runs (empty on timeout)."""
    params = {"reservation_id": reservation_id, "limit": 200}
    if status is not None:
        params["status"] = status
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/execution/runs", params=params)
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


# --- L2: add_to_vlan driver-result failure -----------------------------------


async def test_l2_add_to_vlan_result_failure_records_failed(
    admin_client, gating_l2_template, fresh_device
):
    """add_to_vlan returning {"success": False, ...} (transport ok, driver-
    reported failure) lands the run at FAILED with the driver's error, not
    SUCCESS. login (not knobbed) still succeeds on the same switch, proving the
    gate is per-action, not switch-wide. (Since issue #442 the flow also drives an
    unknobbed create_vlan first, define-on-allocation; it succeeds and does not
    block the knobbed membership op from being attempted and gated.)"""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(
        admin_client, gating_l2_template["id"], f"mock-l2-393-sw-{suffix}", "add_to_vlan"
    )
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

        failed_add = await _poll_runs(
            admin_client, reservation["id"], "add_to_vlan", status="FAILED"
        )
        assert failed_add, "add_to_vlan driver-result failure must record a FAILED run"
        assert failed_add[0]["error"] == "mock injected failure on add_to_vlan"

        success_login = await _poll_runs(admin_client, reservation["id"], "login", status="SUCCESS")
        assert success_login, "login (not knobbed) must still succeed: the gate is per-action"
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


# --- L3: configure_route driver-result failure (provision) ------------------


async def test_l3_configure_route_result_failure_records_failed(
    admin_client, gating_l3_template, fresh_device
):
    """configure_route returning {"success": False, ...} (transport ok, driver-
    reported failure) lands the run at FAILED with the driver's error."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_device(
        admin_client, gating_l3_template["id"], f"mock-l3-393-sw-{suffix}", "configure_route"
    )
    reservation = None
    connection = None
    topology_id = None
    routes = [{"destination": "10.90.0.0/24", "next_hop": "192.168.90.1", "interface": "eth0"}]
    try:
        await _create_config_version(admin_client, switch["id"], routes)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )

        failed_runs = await _poll_runs(
            admin_client, reservation["id"], "configure_route", status="FAILED"
        )
        assert failed_runs, "configure_route driver-result failure must record a FAILED run"
        assert failed_runs[0]["error"] == "mock injected failure on configure_route"
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


# --- L3: remove_route driver-result failure keeps the pin (deprovision) -----


async def test_l3_remove_route_result_failure_keeps_pin_and_acks(
    admin_client, gating_l3_template, fresh_device
):
    """remove_route returning a driver-reported failure on cancel: the FAILED
    run is recorded with the driver's error, and (Decision 6/7's driver-
    failures-ACK posture) the reservation still reaches CANCELLED rather than
    getting stuck retrying the whole message. The route pin itself staying
    ACTIVE is pinned directly against the ledger at the unit level (see
    test_nats_consumer_ledger_teardown.py's
    test_l3_teardown_driver_failure_keeps_pin_and_lands_failed_released); there
    is no REST surface for route_assignments to reassert it live."""
    suffix = uuid.uuid4().hex[:8]
    # Only remove_route is knobbed: configure_route must succeed so there is a
    # pin to attempt removing.
    switch = await _create_device(
        admin_client, gating_l3_template["id"], f"mock-l3-393-sw-{suffix}", "remove_route"
    )
    connection = None
    topology_id = None
    routes = [{"destination": "10.91.0.0/24", "next_hop": "192.168.91.1", "interface": "eth0"}]
    try:
        await _create_config_version(admin_client, switch["id"], routes)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_edge(fresh_device["id"], switch["id"])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )

        assert await _poll_runs(
            admin_client, reservation["id"], "configure_route", status="SUCCESS"
        ), "reservation never provisioned the route, cannot test the removal failure"

        resp = await admin_client.delete(f"/reservations/{reservation['id']}")
        assert resp.status_code == 204, resp.text

        failed_remove = await _poll_runs(
            admin_client, reservation["id"], "remove_route", status="FAILED"
        )
        assert failed_remove, "remove_route driver-result failure must record a FAILED run"
        assert failed_remove[0]["error"] == "mock injected failure on remove_route"

        # A driver-result failure ACKs (Decision 7): the reservation still
        # reaches CANCELLED, it does not hang PENDING_PROVISION-adjacent retrying
        # the whole NATS message.
        assert await _poll_reservation_status(admin_client, reservation["id"], "CANCELLED"), (
            "reservation must still reach CANCELLED after a driver-result failure"
        )
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
