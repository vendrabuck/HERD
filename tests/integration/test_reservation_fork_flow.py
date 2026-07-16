"""Integration coverage for the user-facing reservation fork endpoints (#25 P3a phase 3).

Two end-to-end flows over a running HERD stack:

- The lifecycle flow (reservations -> cabling): reserve and activate a topology,
  read its fork through the new user endpoint, draft-edit the canvas, save-reconcile
  it (asserting the released/built delta shape and a new version), then complete the
  reservation and assert the fork is ARCHIVED and further mutations are refused.
- The backfill / standing-reconciler proof: manufacture a zombie (an ACTIVE fork whose
  reservation is already terminal) via the cabling internal fork-create, then let the
  reservations expiration sweep's archive reconciler freeze it, polling for ARCHIVED.

Both self-seed via the conftest fixtures and clean up in try/finally. They require a
running HERD stack; without one they error at connect time, which is expected. The
backfill proof drives cabling's X-Internal-Token endpoints directly, reading
INTERNAL_API_TOKEN from the repo .env (loaded by conftest), because a genuine zombie
cannot be produced through the user-facing teardown paths (those now always archive).
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.asyncio

INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")


def _canvas_with_edge(device_a_id: str, device_b_id: str) -> dict:
    """Minimal React Flow canvas: two device nodes joined by one committed L1 edge."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": device_a_id}}},
            {"id": "nB", "data": {"device": {"id": device_b_id}}},
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


def _empty_canvas() -> dict:
    return {"nodes": [], "edges": []}


async def _create_connection(client, device_a_id: str, device_b_id: str) -> str:
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": device_a_id,
            "port_a": "eth1",
            "device_b_id": device_b_id,
            "port_b": "eth1",
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def _create_topology_with_canvas(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies",
        json={"name": f"int-fork-{uuid.uuid4().hex[:8]}"},
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


def _reservation_body(device_ids: list[str], topology_id: str | None) -> dict:
    now = datetime.now(timezone.utc)
    body: dict = {
        "device_ids": device_ids,
        "purpose": "fork endpoints integration",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if topology_id is not None:
        body["topology_id"] = topology_id
    return body


async def _create_reservation(client, device_ids: list[str], topology_id: str | None) -> str:
    resp = await client.post("/reservations/", json=_reservation_body(device_ids, topology_id))
    assert resp.status_code == 201, f"reservation create failed: {resp.status_code}: {resp.text}"
    return resp.json()["id"]


async def test_fork_lifecycle_read_edit_save_archive(admin_client, fresh_devices):
    """Reserve, read the fork, draft-edit, save-reconcile, complete, assert ARCHIVED."""
    devices = await fresh_devices(2)
    a_id, b_id = devices[0]["id"], devices[1]["id"]

    connection_id = await _create_connection(admin_client, a_id, b_id)
    topology_id = await _create_topology_with_canvas(
        admin_client, _canvas_with_edge(a_id, b_id)
    )
    reservation_id: str | None = None
    try:
        reservation_id = await _create_reservation(admin_client, [a_id, b_id], topology_id)

        # Read the fork through the new user-facing endpoint. Activation auto-creates
        # it, so this returns 200 with an ACTIVE fork carrying the seeded wiring.
        got = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert got.status_code == 200, got.text
        fork = got.json()
        assert fork["status"] == "ACTIVE"

        # Loose draft edit: clears the wiring on the fork canvas only, no reconcile.
        drafted = await admin_client.put(
            f"/reservations/{reservation_id}/fork/canvas",
            json={"canvas_data": _empty_canvas()},
        )
        assert drafted.status_code == 200, drafted.text

        # Save-reconcile the emptied canvas: the seeded wire is released, none built,
        # and a new fork version is appended.
        saved = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": _empty_canvas()},
        )
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert "released" in result and "built" in result
        assert result["built"] == []
        assert result["version_number"] >= 2

        # Complete the reservation: teardown archives the fork as the as-built record.
        released = await admin_client.put(f"/reservations/{reservation_id}/release")
        assert released.status_code == 200, released.text

        # The as-built is still readable after the reservation ends, and is ARCHIVED.
        after = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ARCHIVED"

        # Mutations on the ended reservation's fork are refused (409).
        refused = await admin_client.put(
            f"/reservations/{reservation_id}/fork/canvas",
            json={"canvas_data": _empty_canvas()},
        )
        assert refused.status_code == 409, refused.text
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_id}")


async def test_standing_reconciler_archives_zombie_fork(admin_client, base_url, fresh_devices):
    """Backfill proof: an ACTIVE fork whose reservation is terminal gets archived.

    A topology-less reservation creates no fork at activation, so completing it leaves
    no fork behind. We then create a fork for that already-terminal reservation via
    cabling's internal endpoint, manufacturing exactly the zombie the standing
    reconciler exists to clean up (a crash-window or pre-phase-3 fork). The reconciler
    runs on the expiration loop; poll for the fork flipping to ARCHIVED.
    """
    if not INTERNAL_TOKEN:
        pytest.skip("INTERNAL_API_TOKEN not available; cannot drive the internal fork endpoints")

    devices = await fresh_devices(1)
    a_id = devices[0]["id"]

    # A reservation with no topology: activates ACTIVE, creates no fork.
    reservation_id = await _create_reservation(admin_client, [a_id], None)
    # End it so its status is terminal (COMPLETED) with still no fork.
    released = await admin_client.put(f"/reservations/{reservation_id}/release")
    assert released.status_code == 200, released.text

    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=15.0) as raw:
        # Manufacture the zombie: an ACTIVE fork for the already-COMPLETED reservation.
        created = await raw.post(
            "/cabling/internal/forks",
            json={"reservation_id": reservation_id},
            headers=headers,
        )
        assert created.status_code == 201, created.text

        # The reconciler runs on the expiration sweep. The dev/test stack pins
        # EXPIRATION_INTERVAL_SECONDS to 5 in docker-compose.override.yml so this
        # poll fits inside the suite's --timeout=30 cap; against a stack running
        # the 60s production default this test would time out by design.
        archived = False
        for _ in range(8):
            got = await raw.get(f"/cabling/internal/forks/{reservation_id}", headers=headers)
            if got.status_code == 200 and got.json()["status"] == "ARCHIVED":
                archived = True
                break
            await asyncio.sleep(3)
        assert archived, "standing reconciler did not archive the zombie fork within the budget"
