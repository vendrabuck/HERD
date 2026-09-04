"""Integration coverage for POST /api/ai/commit.

The commit endpoint writes proposals to inventory + cabling + reservations.
It does NOT call an LLM, so it does not gate on ai_is_configured(). It does
require a valid bearer token. These tests pin the auth and validation
contract without depending on the live AI provider being configured.

The full happy-path commit flow (generate -> commit -> resulting topology
and reservation) is intentionally NOT covered here; that path requires a
configured AI provider and is exercised end-to-end in test_ai_status.py's
generate-succeeds test. This file rounds out the negative-path coverage
which has been missing from the integration tier entirely.

test_commit_with_element_attachment_produces_valid_topology (issue #632) IS
a happy-path test, but it needs no LLM: it posts a CommitRequest directly
(the shape an accepted AI proposal would build), so it belongs here rather
than in test_ai_status.py's generate-gated tier.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_commit_requires_auth(base_url):
    """No bearer token: 401 (or 403), regardless of body validity."""
    now = datetime.now(timezone.utc)
    body = {
        "topology_name": "noop",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "devices": [],
        "edges": [],
    }
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(f"{base_url}/ai/commit", json=body)
    assert resp.status_code in (401, 403), (
        f"expected 401/403 without auth, got {resp.status_code}: {resp.text}"
    )


async def test_commit_empty_devices_rejected(base_url, user_token):
    """The schema enforces devices is non-empty; the validator returns 422."""
    now = datetime.now(timezone.utc)
    body = {
        "topology_name": "empty-proposal",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "devices": [],
        "edges": [],
    }
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/commit",
            json=body,
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 422, f"expected 422 for empty devices, got {resp.status_code}"


async def test_commit_end_before_start_rejected(base_url, user_token):
    """end_time must be after start_time; validator returns 422."""
    now = datetime.now(timezone.utc)
    body = {
        "topology_name": "bad-window",
        "start_time": now.isoformat(),
        "end_time": (now - timedelta(hours=1)).isoformat(),
        "devices": [
            {"role": "dut", "device_id": "00000000-0000-0000-0000-000000000000"}
        ],
        "edges": [],
    }
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/commit",
            json=body,
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 422, (
        f"expected 422 for end<=start, got {resp.status_code}: {resp.text}"
    )


async def test_commit_missing_topology_name_rejected(base_url, user_token):
    """topology_name is required (min_length=1)."""
    now = datetime.now(timezone.utc)
    body = {
        # topology_name omitted
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "devices": [
            {"role": "dut", "device_id": "00000000-0000-0000-0000-000000000000"}
        ],
        "edges": [],
    }
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/ai/commit",
            json=body,
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 422, (
        f"expected 422 for missing topology_name, got {resp.status_code}: {resp.text}"
    )


async def test_commit_does_not_gate_on_ai_provider(base_url, user_token, fresh_device):
    """The commit endpoint is data-only; even without the AI provider
    configured, an authenticated caller reaches the business logic (rather
    than a 503 feature-gate). We send a payload that will pass the schema
    and probe whether the response is anything other than 503. The exact
    outcome (200 or a business error) depends on user_token's permissions
    on the seeded device, which we do not assert here.
    """
    now = datetime.now(timezone.utc)
    body = {
        "topology_name": f"int-commit-probe-{now.timestamp():.0f}",
        "purpose": "commit-gate integration probe",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "devices": [
            {"role": "dut", "device_id": fresh_device["id"]},
        ],
        "edges": [],
        "apply_configs": False,
    }
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/ai/commit",
            json=body,
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code != 503, (
        f"commit should not gate on ai_is_configured(); got 503: {resp.text}"
    )


async def test_commit_with_element_attachment_produces_valid_topology(
    admin_client, base_url, user_token, visible_fresh_device
):
    """A CommitRequest carrying one network element and a device-to-element
    edge (issue #632) commits a topology whose canvas_data holds the
    networkElementNode and an attachment edge with a real, non-empty
    source_port_name (D2: the committer picks the device's port), and the
    topology validator reports it valid with no invalid edges (ADR 0012:
    element edges bypass pathfinding entirely, so nothing to fail).
    """
    suffix = uuid.uuid4().hex[:8]

    # A real inventory port on the device: the committer fetches
    # GET /devices/{id}/ports to pick the attachment's device-side port, and
    # a device with zero ports would have the edge silently skipped.
    port_template_resp = await admin_client.post(
        "/inventory/templates",
        json={
            "name": f"int-ai-port-tpl-{suffix}",
            "template_type": "port",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "note", "label": "Note", "type": "string"}],
                }
            ],
        },
    )
    assert port_template_resp.status_code == 201, port_template_resp.text
    port_template_id = port_template_resp.json()["id"]

    port_resp = await admin_client.post(
        f"/inventory/devices/{visible_fresh_device['id']}/ports",
        json={"name": "eth0", "template_id": port_template_id, "field_data": {}},
    )
    assert port_resp.status_code == 201, port_resp.text
    port_id = port_resp.json()["id"]

    now = datetime.now(timezone.utc)
    body = {
        "topology_name": f"int-ai-element-{suffix}",
        "purpose": "ai element attachment integration probe",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "devices": [{"role": "dut", "device_id": visible_fresh_device["id"]}],
        "elements": [
            {
                "role": "mgmt-seg",
                "element_type": "vlan_segment",
                "label": "Mgmt VLAN",
                "attrs": {"vlan_id": 100},
            }
        ],
        "edges": [{"source_role": "dut", "target_role": "mgmt-seg", "layer": "L2"}],
        "apply_configs": False,
    }

    topology_id: str | None = None
    reservation_id: str | None = None
    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/ai/commit",
                json=body,
                headers={"Authorization": f"Bearer {user_token}"},
            )
        assert resp.status_code == 200, f"commit failed: {resp.status_code}: {resp.text}"
        commit_result = resp.json()
        topology_id = commit_result["topology_id"]
        reservation_id = commit_result["reservation_id"]

        # Read the committed topology back and inspect canvas_data directly.
        topo_resp = await admin_client.get(f"/cabling/topologies/{topology_id}")
        assert topo_resp.status_code == 200, topo_resp.text
        canvas = topo_resp.json()["canvas_data"]

        element_nodes = [n for n in canvas["nodes"] if n["type"] == "networkElementNode"]
        device_nodes = [n for n in canvas["nodes"] if n["type"] == "deviceNode"]
        assert len(element_nodes) == 1, canvas
        assert len(device_nodes) == 1, canvas
        assert element_nodes[0]["data"]["element"]["element_type"] == "vlan_segment"

        assert len(canvas["edges"]) == 1, canvas
        edge = canvas["edges"][0]
        assert edge["source"] == device_nodes[0]["id"]
        assert edge["target"] == element_nodes[0]["id"]
        assert edge["data"].get("source_port_name"), "attachment edge must name a real port"
        assert edge["data"]["source_port_name"] == "eth0"

        # The user-facing validator (creator-or-admin gated) reports the
        # topology valid: the element attachment bypasses pathfinding
        # entirely (ADR 0012), so there is nothing for it to fail.
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=30.0,
        ) as uclient:
            validate_resp = await uclient.post(f"/cabling/topologies/{topology_id}/validate")
        assert validate_resp.status_code == 200, validate_resp.text
        validation = validate_resp.json()
        assert validation["valid"] is True, validation
        assert validation["invalid_edges"] == []
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        # The port must go before the template that defines it, or the
        # template delete 409s ("ports still reference it"); the device
        # itself (and this port, cascading) is cleaned up separately by the
        # fresh_device fixture behind visible_fresh_device.
        await admin_client.delete(f"/inventory/ports/{port_id}")
        await admin_client.delete(f"/inventory/templates/{port_template_id}")
