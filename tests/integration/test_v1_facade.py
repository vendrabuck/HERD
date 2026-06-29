"""Integration tests for the versioned /api/v1 reservation facade (issue #33).

The facade is a thin, stable external surface that forwards the caller's JWT to
the internal reservations service. These tests drive it through Traefik on the
running stack, including the machine-principal flow: mint an API token, exchange
it for a JWT, and reserve via /api/v1 with no interactive login.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.asyncio


def _reservation_body(device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "device_ids": [device_id],
        "purpose": "v1 facade integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }


async def test_v1_reserve_status_list_release(admin_client, fresh_device):
    """Reserve, read status, list, and release a reservation through /api/v1."""
    create = await admin_client.post("/v1/reservations", json=_reservation_body(fresh_device["id"]))
    assert create.status_code == 201, create.text
    res = create.json()
    # Frozen v1 shape.
    assert set(res) >= {"id", "status", "device_ids", "start_time", "end_time", "created_at"}
    assert fresh_device["id"] in res["device_ids"]
    reservation_id = res["id"]

    try:
        status_resp = await admin_client.get(f"/v1/reservations/{reservation_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["id"] == reservation_id

        listed = await admin_client.get("/v1/reservations")
        assert listed.status_code == 200
        assert any(item["id"] == reservation_id for item in listed.json()["items"])

        release = await admin_client.put(f"/v1/reservations/{reservation_id}/release")
        assert release.status_code == 200
        assert release.json()["status"] in ("COMPLETED", "ACTIVE")
    finally:
        await admin_client.delete(f"/v1/reservations/{reservation_id}")


async def test_v1_requires_authentication(base_url, fresh_device):
    """The facade rejects an unauthenticated caller with 401 (auth at the edge)."""
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=30.0) as anon:
        resp = await anon.post("/v1/reservations", json=_reservation_body(fresh_device["id"]))
    assert resp.status_code in (401, 403)


async def test_v1_propagates_not_found(admin_client):
    """An unknown reservation id surfaces the upstream 404 unchanged."""
    resp = await admin_client.get("/v1/reservations/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_machine_principal_reserves_via_token_exchange(admin_client, base_url, fresh_device):
    """Headline #33 AC: an API token, exchanged for a JWT, reserves via /api/v1
    with no interactive login."""
    me = await admin_client.get("/auth/me")
    assert me.status_code == 200
    principal_id = me.json()["id"]

    token_id = None
    reservation_id = None
    try:
        created = await admin_client.post(
            "/auth/tokens",
            json={"name": "v1-integration-token", "principal_id": principal_id, "role": "admin"},
        )
        assert created.status_code == 201, created.text
        token_id = created.json()["id"]
        raw_token = created.json()["token"]

        # Exchange the API token for a short-lived JWT (no interactive login).
        async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=30.0) as anon:
            exchanged = await anon.post("/auth/tokens/exchange", json={"token": raw_token})
            assert exchanged.status_code == 200, exchanged.text
            access = exchanged.json()["access_token"]

            # Use the exchanged JWT to reserve through /api/v1.
            machine = await anon.post(
                "/v1/reservations",
                json=_reservation_body(fresh_device["id"]),
                headers={"Authorization": f"Bearer {access}"},
            )
        assert machine.status_code == 201, machine.text
        reservation_id = machine.json()["id"]
        assert fresh_device["id"] in machine.json()["device_ids"]
    finally:
        if reservation_id:
            await admin_client.delete(f"/v1/reservations/{reservation_id}")
        if token_id:
            await admin_client.delete(f"/auth/tokens/{token_id}")
