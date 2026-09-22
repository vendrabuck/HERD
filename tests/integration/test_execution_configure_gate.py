"""Integration coverage for the execution-side configure capability gate
(issue #870) through the real AI commit path.

Issue #839 gated config APPLY behind the driver's contract, but only in
inventory's two apply routes. Execution itself accepted action="configure"
against ANY driver connection type, and the AI commit path
(services/ai-orchestrator/app/services/committer.py's `_apply_configs`) POSTs
`configure` straight to execution's `/execute`, bypassing inventory's gate
entirely. A Layer 3 Switch is exactly the kind of device the AI proposes
configs for (ADR 0014 stores L3 routing intent as a device config), so this
drives a real POST /ai/commit with apply_configs=true against a real
mock_l3-backed device and asserts:

  - the commit itself still succeeds (topology + reservation created: a
    config-apply failure never blocks the commit, per commit_proposal's
    existing contract);
  - the per-device config_results entry is "failed" (not counted as applied)
    with the plain-words message from execution's structured 409, not a
    stringified dict; and
  - no ExecutionRun row was ever created for the device (GET /execution/runs
    reports total=0), proving the refusal happened before create_execution_run
    and before any driver load, deep inside a route this test never reaches.

The commit endpoint does not call an LLM (test_ai_commit_route.py's docstring
notes the same), so this needs no configured AI provider. Uses the checked-in
mock_l3 driver via the shared _l3_helpers, matching test_l3_route_provisioning.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ._l3_helpers import create_device as _create_device
from ._l3_helpers import create_l3_driver, create_l3_template

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="session")
async def gate_l3_driver(base_url, admin_token):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        driver = await create_l3_driver(client, f"gate-mock-l3-{uuid.uuid4().hex[:8]}")
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def gate_l3_template(base_url, admin_token, gate_l3_driver):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        template = await create_l3_template(
            client, gate_l3_driver["id"], f"gate-l3-tpl-{uuid.uuid4().hex[:8]}"
        )
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


async def test_ai_commit_apply_configs_refuses_l3_device_via_execution_gate(
    admin_client, base_url, admin_token, gate_l3_template
):
    device = await _create_device(
        admin_client, gate_l3_template["id"], f"gate-l3-dev-{uuid.uuid4().hex[:8]}"
    )
    topology_id: str | None = None
    reservation_id: str | None = None
    try:
        now = datetime.now(timezone.utc)
        body = {
            "topology_name": f"int-execution-gate-{uuid.uuid4().hex[:8]}",
            "purpose": "issue #870 execution configure-gate integration probe",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
            "devices": [
                {
                    "role": "l3sw",
                    "device_id": device["id"],
                    "config": {"routes": []},
                    "connection_type": "Layer 3 Switch",
                }
            ],
            "edges": [],
            "apply_configs": True,
        }
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/ai/commit",
                json=body,
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert resp.status_code == 200, f"commit failed: {resp.status_code}: {resp.text}"
        commit_result = resp.json()
        topology_id = commit_result["topology_id"]
        reservation_id = commit_result["reservation_id"]

        config_results = commit_result["config_results"]
        assert len(config_results) == 1, config_results
        result = config_results[0]
        assert result["role"] == "l3sw"
        assert result["status"] == "failed"
        assert result["run_id"] is None
        # Plain words from execution's structured 409 detail (issue #870), not
        # a Python-dict-repr fallback: _detail(resp) would render the whole
        # {"error": "driver_cannot_configure", ...} object as text.
        assert "driver_cannot_configure" not in (result["error"] or "")
        assert "has no configure method" in (result["error"] or "")

        # No ExecutionRun row was created: the refusal landed before
        # create_execution_run, not after a failed driver call.
        runs_resp = await admin_client.get("/execution/runs", params={"device_id": device["id"]})
        assert runs_resp.status_code == 200, runs_resp.text
        assert runs_resp.json()["total"] == 0, runs_resp.json()
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/inventory/devices/{device['id']}")
