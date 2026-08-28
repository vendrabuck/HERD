"""Integration coverage for the user-facing reservation fork endpoints (#25 P3a phase 3).

Three end-to-end flows over a running HERD stack:

- The lifecycle flow (reservations -> cabling): reserve and activate a topology,
  read its fork through the new user endpoint, draft-edit the canvas, save-reconcile
  it (asserting the released/built delta shape and a new version), then complete the
  reservation and assert the fork is ARCHIVED and further mutations are refused.
- The backfill / standing-reconciler proof: manufacture a zombie (an ACTIVE fork whose
  reservation is already terminal) via the cabling internal fork-create, then let the
  reservations expiration sweep's archive reconciler freeze it, polling for ARCHIVED.
- The restore-to-draft flow (issue #622, revised after PR #623 review): save twice,
  restore an earlier version, assert the restore only rewrote the draft canvas,
  appended NO fork_versions row (so cabling's latest fork_version does not advance
  and the standing wiring-heal reconciler cannot mistake it for a missed save), and
  staged NO wiring change (the applied wiring stays exactly what the last save
  left); then save again and assert THAT save both advances the version and
  releases the wire that only the intermediate version had.

All three self-seed via the conftest fixtures and clean up in try/finally. They
require a running HERD stack; without one they error at connect time, which is
expected. The backfill proof drives cabling's X-Internal-Token endpoints directly,
reading INTERNAL_API_TOKEN from the repo .env (loaded by conftest), because a genuine
zombie cannot be produced through the user-facing teardown paths (those now always
archive).
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
    topology_id = await _create_topology_with_canvas(admin_client, _canvas_with_edge(a_id, b_id))
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


async def _wiring_status(client, reservation_id: str) -> dict:
    resp = await client.get(f"/reservations/{reservation_id}/wiring-status")
    resp.raise_for_status()
    return resp.json()


def _endpoint_pair(delta: dict) -> frozenset:
    return frozenset(
        {(delta["device_a_id"], delta["port_a"]), (delta["device_b_id"], delta["port_b"])}
    )


async def test_fork_restore_to_draft_is_canvas_only_then_save_reconciles(
    admin_client, fresh_devices
):
    """Restore-to-draft (issue #622, revised after PR #623 review) never wires and
    never appends a fork_versions row of its own.

    Sequence: activate onto a topology wiring A-B (fork v1, no wiring built yet: the
    activation snapshot alone stages nothing). Save #1 (v2) moves the canvas to A-C,
    which is the first save ever for this fork, so it builds A-C and releases
    nothing. Save #2 (v3) resaves the SAME A-C canvas (an unchanged re-save, per the
    cabling reconcile's own convention): the applied wiring stays A-C, only the
    version number advances. Restoring v1 must then only rewrite the fork's draft
    canvas back to A-B and set draft_restored_from_id: it appends NO version (the
    fork's version list stays at exactly v1/v2/v3, proving cabling's own
    latest-fork-version count never advanced, so the standing wiring-heal reconciler
    has nothing to misread as a missed save) and stages NO wiring_changed event, so
    the applied wiring (still A-C, v2's wire, unchanged since) is untouched; the
    wiring-status read proves this directly rather than trusting the restore
    response. A follow-up save of the restored draft (A-B) is the one that actually
    advances the version (to v4, since restore consumed no number) and carries
    restored_from_id = v1's id; it reconciles for real, releasing the
    version-2-only wire A-C and building A-B.
    """
    devices = await fresh_devices(3)
    a_id, b_id, c_id = devices[0]["id"], devices[1]["id"], devices[2]["id"]

    connection_ab = await _create_connection(admin_client, a_id, b_id)
    connection_ac_resp = await admin_client.post(
        "/cabling/connections",
        json={
            "device_a_id": a_id,
            "port_a": "eth2",
            "device_b_id": c_id,
            "port_b": "eth1",
            "connection_type": "L1",
        },
    )
    connection_ac_resp.raise_for_status()
    connection_ac = connection_ac_resp.json()["id"]

    canvas_ab = _canvas_with_edge(a_id, b_id)
    canvas_ac = _canvas_with_edge(a_id, c_id)
    topology_id = await _create_topology_with_canvas(admin_client, canvas_ab)
    reservation_id: str | None = None
    try:
        reservation_id = await _create_reservation(admin_client, [a_id, b_id, c_id], topology_id)

        # v1: the activation snapshot. No save has run yet, so nothing is wired.
        fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert fork.status_code == 200, fork.text
        assert fork.json()["canvas_data"] == canvas_ab
        v1 = next(v for v in fork.json()["versions"] if v["version_number"] == 1)
        baseline_status = await _wiring_status(admin_client, reservation_id)
        assert baseline_status.get("connections", []) == []

        # Save #1 -> v2: first-ever save, moves the canvas (and the wiring) to A-C.
        save_1 = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas_ac},
        )
        assert save_1.status_code == 200, save_1.text
        body_1 = save_1.json()
        assert body_1["version_number"] == 2
        assert body_1["released"] == []
        assert len(body_1["built"]) == 1
        assert _endpoint_pair(body_1["built"][0]) == frozenset({(a_id, "eth2"), (c_id, "eth1")})

        # Save #2 -> v3: resave the identical A-C canvas. unchanged, no new delta.
        save_2 = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas_ac},
        )
        assert save_2.status_code == 200, save_2.text
        body_2 = save_2.json()
        assert body_2["version_number"] == 3
        assert body_2["released"] == []
        assert body_2["built"] == []
        assert body_2["unchanged_count"] == 1

        # Snapshot wiring-status AND the fork's version count right before the
        # restore, so both "restore changes nothing" assertions below are real
        # before/after comparisons, not guesses.
        pre_restore_status = await _wiring_status(admin_client, reservation_id)
        pre_restore_fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert pre_restore_fork.status_code == 200, pre_restore_fork.text
        pre_restore_versions = pre_restore_fork.json()["versions"]
        assert len(pre_restore_versions) == 3
        assert max(v["version_number"] for v in pre_restore_versions) == 3

        restored = await admin_client.post(
            f"/reservations/{reservation_id}/fork/versions/{v1['id']}/restore"
        )
        assert restored.status_code == 200, restored.text
        restore_body = restored.json()
        assert restore_body["draft_restored_from_id"] == v1["id"]
        # ForkCanvasUpdateResponse's shape plus the marker: no "version" key, and
        # specifically no version_number, since restore appends none.
        assert "version" not in restore_body

        # The draft is back to v1's canvas, and the marker is visible on the fork...
        after_restore_fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert after_restore_fork.status_code == 200, after_restore_fork.text
        after_restore_body = after_restore_fork.json()
        assert after_restore_body["canvas_data"] == canvas_ab
        assert after_restore_body["draft_restored_from_id"] == v1["id"]

        # ...but cabling's own latest fork_version never advanced: still exactly
        # v1/v2/v3, proving restore appended no fork_versions row. This is the
        # PR #623 review fix: an appended version here would have falsely tripped
        # the standing wiring-heal reconciler (ADR 0007 Decision 2), which trusts a
        # fork_version advance to mean a save's staging was missed.
        after_restore_versions = after_restore_body["versions"]
        assert len(after_restore_versions) == 3, (
            f"restore must append no fork_versions row, got {after_restore_versions}"
        )
        assert max(v["version_number"] for v in after_restore_versions) == 3
        assert {v["id"] for v in after_restore_versions} == {v["id"] for v in pre_restore_versions}

        # And the APPLIED wiring never moved either: restore's own request/commit
        # path never calls stage_wiring_changed (unlike save), so a read immediately
        # after the restore response matches the pre-restore snapshot exactly.
        immediately_after = await _wiring_status(admin_client, reservation_id)
        assert immediately_after == pre_restore_status, (
            "wiring-status changed across the restore call; restore must be canvas-only"
        )

        # Now save the restored draft for real: this is the reconcile the user runs
        # deliberately, and the FIRST save to actually advance the version since the
        # restore (to v4, since restore consumed no number). It must carry
        # restored_from_id = v1's id, and release the version-2-only wire A-C while
        # building A-B.
        final_save = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas_ab},
        )
        assert final_save.status_code == 200, final_save.text
        final_body = final_save.json()
        assert final_body["version_number"] == 4
        assert len(final_body["released"]) == 1
        assert _endpoint_pair(final_body["released"][0]) == frozenset(
            {(a_id, "eth2"), (c_id, "eth1")}
        )
        assert len(final_body["built"]) == 1
        assert _endpoint_pair(final_body["built"][0]) == frozenset({(a_id, "eth1"), (b_id, "eth1")})

        # The marker is consumed: the fork row's draft_restored_from_id clears, and
        # the newly appended v4 carries it as its own restored_from_id instead.
        after_save_fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert after_save_fork.status_code == 200, after_save_fork.text
        after_save_body = after_save_fork.json()
        assert after_save_body["draft_restored_from_id"] is None
        v4 = next(v for v in after_save_body["versions"] if v["version_number"] == 4)
        assert v4["restored_from_id"] == v1["id"]
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        await admin_client.delete(f"/cabling/topologies/{topology_id}")
        await admin_client.delete(f"/cabling/connections/{connection_ab}")
        await admin_client.delete(f"/cabling/connections/{connection_ac}")
