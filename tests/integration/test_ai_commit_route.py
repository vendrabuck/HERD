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
"""

from __future__ import annotations

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
