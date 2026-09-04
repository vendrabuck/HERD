"""End-to-end integration: lab purpose classification (issue #646 phases 1 and 3).

Assumes a running HERD stack (make up). Exercises the v1 facade create path,
the PATCH endpoint, and the utilization report's by_purpose breakdown against
a real reservations service and Postgres, not the SQLite unit harness. The
transit-gear test below (phase 3, ADR 0013) additionally drives a real fork
through cabling and a mock L1 switch.
"""

import io
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.asyncio


def _reservation_body(device_id: str, purpose_category: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "purpose classification integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if purpose_category is not None:
        body["purpose_category"] = purpose_category
    return body


async def test_purpose_category_via_v1_facade_read_back_and_patch(admin_client, fresh_device):
    """Create through /api/v1 with a category, read it back through both the
    v1 facade and the interactive reservations endpoint, then clear it via
    PATCH and confirm the interactive read reflects the clear."""
    create = await admin_client.post(
        "/v1/reservations",
        json=_reservation_body(fresh_device["id"], purpose_category="qa_regression"),
    )
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]
    assert create.json()["purpose_category"] == "qa_regression"

    try:
        v1_get = await admin_client.get(f"/v1/reservations/{reservation_id}")
        assert v1_get.status_code == 200
        assert v1_get.json()["purpose_category"] == "qa_regression"

        direct_get = await admin_client.get(f"/reservations/{reservation_id}")
        assert direct_get.status_code == 200
        direct_data = direct_get.json()
        assert direct_data["purpose_category"] == "qa_regression"
        assert direct_data["purpose_category_set_at"] is not None

        patch_resp = await admin_client.patch(
            f"/reservations/{reservation_id}/purpose-category",
            json={"purpose_category": None},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["purpose_category"] is None

        after_clear = await admin_client.get(f"/reservations/{reservation_id}")
        assert after_clear.status_code == 200
        assert after_clear.json()["purpose_category"] is None
        assert after_clear.json()["purpose_category_set_at"] is None
    finally:
        await admin_client.delete(f"/reservations/{reservation_id}")


async def test_purpose_category_unknown_value_rejected_through_facade(admin_client, fresh_device):
    """An unknown category is rejected with the same 422 wording whether the
    caller goes through the v1 facade or the interactive endpoint directly."""
    resp = await admin_client.post(
        "/v1/reservations",
        json=_reservation_body(fresh_device["id"], purpose_category="not_a_real_category"),
    )
    assert resp.status_code == 422
    assert "Unknown purpose_category" in str(resp.json()["detail"])


async def test_utilization_report_by_purpose_includes_classified_reservation(
    admin_client, fresh_device
):
    """The utilization report's by_purpose breakdown (issue #646 phase 1)
    includes a reservation classified through the create path."""
    create = await admin_client.post(
        "/reservations/",
        json=_reservation_body(fresh_device["id"], purpose_category="training"),
    )
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]

    try:
        now = datetime.now(timezone.utc)
        resp = await admin_client.get(
            "/reservations/reports/utilization",
            params={
                "start": (now - timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                "status": "ACTIVE",
            },
        )
        assert resp.status_code == 200, resp.text
        by_purpose = {b["purpose_category"]: b for b in resp.json()["by_purpose"]}
        assert "training" in by_purpose
        assert by_purpose["training"]["reservations"] >= 1
    finally:
        await admin_client.delete(f"/reservations/{reservation_id}")


# --- Transit-gear inheritance (issue #646 phase 3, ADR 0013 "Delivery phases"
# point 3) -----------------------------------------------------------------
#
# Builds a real DUT-switch-DUT path through cabling and a mock L1 switch,
# exactly the shape test_l1_provisioning.py's fork provisioning tests use
# (grepped for "fork" + "mock_l1"): two fresh DUT devices, a device wired to
# the checked-in drivers/mock_l1 driver, cabling connections from each DUT to
# the switch, a topology committing a single DUT-to-DUT canvas edge, and a
# reservation that activates immediately. The l1_driver/l1_template session
# fixtures and the connect/topology helpers are duplicated from
# test_l1_provisioning.py rather than shared via conftest.py, matching this
# suite's existing per-file-duplication convention (see the WiringDialog/
# MultiConnectDialog precedent in CLAUDE.md, tracked there as issue #539: not
# worth a shared module until a third consumer needs it).

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
async def l1_driver(base_url, admin_token):
    """Upload the mock Layer 1 Switch driver once per session."""
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"mock-l1-purpose-{uuid.uuid4().hex[:8]}",
            "connection_type": "Layer 1 Switch",
            "description": "purpose classification transit-gear integration test",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l1_template(base_url, admin_token, l1_driver):
    """A device template wired to the mock L1 driver."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = {
            "name": f"mock-l1-purpose-tmpl-{uuid.uuid4().hex[:8]}",
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


async def _create_switch(client, template_id: str, name: str) -> dict:
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


async def _connect_port(
    client, dut_id: str, dut_port: str, switch_id: str, switch_port: str
) -> dict:
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
        "/cabling/topologies", json={"name": f"int-purpose-transit-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def test_transit_gear_switch_appears_in_by_device_purpose(
    admin_client, l1_template, fresh_devices
):
    """A reservation wired DUT-switch-DUT: the switch is never reserved, only on
    the fork's resolved path, so it must appear in by_device_purpose (and
    by_device) inheriting the reservation's confirmed category, with
    transit_reservations == 1 and transit_device_hours (transit_hours on
    by_device) greater than 0; include_transit=false must drop it entirely,
    since an unreserved device has no row at all under phase-1 semantics."""
    suffix = uuid.uuid4().hex[:8]
    switch = await _create_switch(admin_client, l1_template["id"], f"mock-l1-tg-{suffix}")
    dut_a, dut_b = await fresh_devices(2)
    reservation = None
    connections = []
    topology_id = None
    try:
        connections.append(
            await _connect_port(admin_client, dut_a["id"], "eth0", switch["id"], "p1")
        )
        connections.append(
            await _connect_port(admin_client, dut_b["id"], "eth0", switch["id"], "p2")
        )
        topology_id = await _create_topology(admin_client, _canvas_edge(dut_a["id"], dut_b["id"]))

        now = datetime.now(timezone.utc)
        create = await admin_client.post(
            "/reservations/",
            json={
                "device_ids": [dut_a["id"], dut_b["id"]],
                "topology_id": topology_id,
                "purpose": "transit gear integration test",
                "purpose_category": "qa_regression",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
        )
        assert create.status_code == 201, create.text
        reservation = create.json()
        reservation_id = reservation["id"]
        assert reservation["status"] == "ACTIVE", reservation
        assert reservation["purpose_category"] == "qa_regression"

        # Vacuity guard: the fork must have actually resolved a hop through the
        # switch before asking anything of the report; a silent cabling-side
        # failure to wire through the switch would otherwise be masked as "the
        # report correctly found nothing" rather than caught here directly.
        fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert fork.status_code == 200, fork.text
        fork_connections = fork.json()["connections"]
        fork_device_ids = {
            dev for c in fork_connections for dev in (c["device_a_id"], c["device_b_id"])
        }
        assert switch["id"] in fork_device_ids, (
            f"expected the switch on the fork's resolved path, got {fork_connections}"
        )

        window_start = (now - timedelta(hours=1)).isoformat()
        window_end = (now + timedelta(hours=2)).isoformat()

        resp = await admin_client.get(
            "/reservations/reports/utilization",
            params={"start": window_start, "end": window_end, "status": "ACTIVE"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["transit_included"] is True

        by_device_purpose = {
            (b["device_id"], b["purpose_category"]): b for b in data["by_device_purpose"]
        }
        switch_purpose_row = by_device_purpose.get((switch["id"], "qa_regression"))
        assert switch_purpose_row is not None, (
            f"expected the switch under qa_regression in by_device_purpose, got "
            f"{data['by_device_purpose']}"
        )
        assert switch_purpose_row["transit_reservations"] == 1
        assert switch_purpose_row["transit_device_hours"] > 0

        by_device = {b["device_id"]: b for b in data["by_device"]}
        switch_device_row = by_device.get(switch["id"])
        assert switch_device_row is not None, (
            f"expected the switch in by_device, got {data['by_device']}"
        )
        assert switch_device_row["transit_reservations"] == 1
        assert switch_device_row["transit_hours"] > 0

        # include_transit=false reproduces phase-1 semantics: the switch was
        # never reserved, so with transit inheritance off it has no row at all.
        resp_no_transit = await admin_client.get(
            "/reservations/reports/utilization",
            params={
                "start": window_start,
                "end": window_end,
                "status": "ACTIVE",
                "include_transit": "false",
            },
        )
        assert resp_no_transit.status_code == 200, resp_no_transit.text
        no_transit_data = resp_no_transit.json()
        assert no_transit_data["transit_included"] is False
        assert switch["id"] not in {b["device_id"] for b in no_transit_data["by_device"]}
        assert (switch["id"], "qa_regression") not in {
            (b["device_id"], b["purpose_category"]) for b in no_transit_data["by_device_purpose"]
        }
    finally:
        if reservation:
            await admin_client.delete(f"/reservations/{reservation['id']}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        for conn in connections:
            await admin_client.delete(f"/cabling/connections/{conn['id']}")
        await admin_client.delete(f"/inventory/devices/{switch['id']}")


async def test_purely_reserved_reservation_has_zero_transit(admin_client, fresh_devices):
    """No fork ever forms an intermediate hop here (a single directly-cabled
    DUT reserved alone, no topology): by_device_purpose shows zero transit and
    transit_included echoes the flag either way. The minimal-fixture backstop
    for the transit-gear assertions above, independent of the mock L1 switch."""
    (dut,) = await fresh_devices(1)
    now = datetime.now(timezone.utc)
    create = await admin_client.post(
        "/reservations/",
        json={
            "device_ids": [dut["id"]],
            "purpose": "transit gear zero-case integration test",
            "purpose_category": "other",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]
    try:
        window_start = (now - timedelta(hours=1)).isoformat()
        window_end = (now + timedelta(hours=2)).isoformat()

        resp = await admin_client.get(
            "/reservations/reports/utilization",
            params={"start": window_start, "end": window_end, "status": "ACTIVE"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["transit_included"] is True
        by_device_purpose = {
            (b["device_id"], b["purpose_category"]): b for b in data["by_device_purpose"]
        }
        row = by_device_purpose.get((dut["id"], "other"))
        assert row is not None
        assert row["transit_reservations"] == 0
        assert row["transit_device_hours"] == 0

        resp_no_transit = await admin_client.get(
            "/reservations/reports/utilization",
            params={
                "start": window_start,
                "end": window_end,
                "status": "ACTIVE",
                "include_transit": "false",
            },
        )
        assert resp_no_transit.status_code == 200, resp_no_transit.text
        assert resp_no_transit.json()["transit_included"] is False
    finally:
        await admin_client.delete(f"/reservations/{reservation_id}")
