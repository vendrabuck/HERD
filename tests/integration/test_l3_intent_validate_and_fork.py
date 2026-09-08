"""Integration tests for L3 routing-intent validate and the fork write path
(ADR 0014 phase 1, issue #34).

Reuses test_l3_route_provisioning.py's setup helpers and fixture shapes (mock_l3
driver upload, device/connection/topology/reservation helpers, config-version
creation) rather than duplicating the provisioning proof: this file's own concern
is the validate pass and fork_l3_routes, not execution's driver calls (phase 3,
not yet built). `l3_driver`/`l3_template` are re-declared locally (session-scoped
fixtures do not cross test modules without a shared conftest plugin), each
uploading its own uniquely-named driver so running both files together is safe.
"""

import io
import os
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.asyncio

_MOCK_L3_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l3"

# The Layer 3 Switch config schema (services/common/herd_common/device_config.py)
# requires "zone" on every interfaces item (additionalProperties: False, "name"
# and "zone" both required) since the mock_l3 driver publishes no config_schema()
# of its own and inventory falls back to that registry schema. "ip" stays
# prefixed (a bare address has no real prefix length, which the L3 validation
# pass's l3_next_hop_unverifiable check would then correctly refuse).
INTERFACES = [{"name": "eth0", "ip": "10.0.0.1/24", "zone": "trust"}]
VALID_ROUTE = {"destination": "10.20.0.0/24", "next_hop": "10.0.0.2", "interface": "eth0"}
BAD_DESTINATION_ROUTE = {"destination": "not-an-ip", "interface": "eth0"}


def _mock_l3_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L3_DIR / name, arcname=name)
    return buf.getvalue()


@pytest.fixture(scope="session")
async def l3_driver(base_url, admin_token):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        files = {"file": ("mock_l3.tar.gz", _mock_l3_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l3-intent-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 3 Switch",
            "description": "integration mock L3 switch driver (intent tests)",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l3_template(base_url, admin_token, l3_driver):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        payload = {
            "name": f"mock-l3-intent-tmpl-{uuid.uuid4().hex[:8]}",
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


def _canvas_with_l3(dut_id: str, switch_id: str, routes: list[dict] | None) -> dict:
    """A committed one-edge canvas wiring a DUT to a switch node that optionally
    carries data.l3 routing intent (ADR 0014 Decision 1)."""
    switch_data: dict = {"device": {"id": switch_id}}
    if routes is not None:
        switch_data["l3"] = {"routes": routes}
    return {
        "nodes": [
            {"id": "nDut", "data": {"device": {"id": dut_id}}},
            {"id": "nSwitch", "data": switch_data},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nDut",
                "target": "nSwitch",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-l3-intent-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _create_config_version(client, device_id: str, interfaces: list[dict]) -> dict:
    resp = await client.post(
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": {"interfaces": interfaces}, "description": "l3 intent integration config"},
    )
    # Explicit over raise_for_status alone: a schema-validation 422 here (the
    # payload not matching the Layer 3 Switch registry schema in
    # herd_common/device_config.py, which applies since mock_l3 publishes no
    # config_schema() of its own) is a test-setup bug, not a driver/network
    # failure, and deserves its own clear assertion message.
    assert resp.status_code == 201, f"config-version create failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _create_reservation(client, device_ids: list[str], topology_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": "l3 routing intent integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def test_validate_valid_l3_intent(admin_client, l3_template, fresh_device):
    """A topology whose switch carries one valid route validates true, with an
    empty invalid_routes list."""
    switch = await _create_device(
        admin_client, l3_template["id"], f"mock-l3-intent-{uuid.uuid4().hex[:8]}"
    )
    connection = None
    topology_id = None
    try:
        await _create_config_version(admin_client, switch["id"], INTERFACES)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_with_l3(fresh_device["id"], switch["id"], [VALID_ROUTE])
        )

        resp = await admin_client.post(f"/cabling/topologies/{topology_id}/validate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is True
        assert body["invalid_routes"] == []
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_validate_bad_destination_reports_reason(admin_client, l3_template, fresh_device):
    """A topology with a bad destination reports l3_bad_destination and is invalid."""
    switch = await _create_device(
        admin_client, l3_template["id"], f"mock-l3-intent-{uuid.uuid4().hex[:8]}"
    )
    connection = None
    topology_id = None
    try:
        await _create_config_version(admin_client, switch["id"], INTERFACES)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client,
            _canvas_with_l3(fresh_device["id"], switch["id"], [BAD_DESTINATION_ROUTE]),
        )

        resp = await admin_client.post(f"/cabling/topologies/{topology_id}/validate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is False
        assert len(body["invalid_routes"]) == 1
        assert body["invalid_routes"][0]["reason"] == "l3_bad_destination"
        assert body["invalid_routes"][0]["device_id"] == switch["id"]
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_commit_reservation_reads_l3_routes_back_and_survives_cancel_archive(
    admin_client, base_url, l3_template, fresh_device
):
    """Committing a reservation against a validly-routed topology resolves the
    intent into the fork; GET /reservations/{id}/fork reads it back. Cancelling
    archives the fork and the rows remain, readable via cabling's internal GET.
    """
    switch = await _create_device(
        admin_client, l3_template["id"], f"mock-l3-intent-{uuid.uuid4().hex[:8]}"
    )
    connection = None
    topology_id = None
    reservation_id = None
    try:
        await _create_config_version(admin_client, switch["id"], INTERFACES)
        connection = await _create_connection(
            admin_client, fresh_device["id"], switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_with_l3(fresh_device["id"], switch["id"], [VALID_ROUTE])
        )
        reservation = await _create_reservation(
            admin_client, [fresh_device["id"], switch["id"]], topology_id
        )
        reservation_id = reservation["id"]

        fork_resp = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert fork_resp.status_code == 200, fork_resp.text
        fork_body = fork_resp.json()
        assert len(fork_body["l3_routes"]) == 1
        route = fork_body["l3_routes"][0]
        assert route["device_id"] == switch["id"]
        assert route["destination"] == VALID_ROUTE["destination"]
        assert route["next_hop"] == VALID_ROUTE["next_hop"]
        assert route["interface"] == VALID_ROUTE["interface"]

        cancel = await admin_client.delete(f"/reservations/{reservation_id}")
        assert cancel.status_code == 204, cancel.text

        internal_token = os.getenv("INTERNAL_API_TOKEN", "")
        if not internal_token:
            pytest.skip("INTERNAL_API_TOKEN not available; cannot read the archived fork directly")
        headers = {"X-Internal-Token": internal_token}
        async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=15.0) as raw:
            archived = await raw.get(f"/cabling/internal/forks/{reservation_id}", headers=headers)
        assert archived.status_code == 200, archived.text
        archived_body = archived.json()
        assert archived_body["status"] == "ARCHIVED"
        assert len(archived_body["l3_routes"]) == 1
        assert archived_body["l3_routes"][0]["destination"] == VALID_ROUTE["destination"]
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")
