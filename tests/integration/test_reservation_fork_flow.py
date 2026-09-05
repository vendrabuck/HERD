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
            json={"reservation_id": reservation_id, "member_device_ids": [a_id]},
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


async def _wait_for_applied_version(
    client, reservation_id: str, version: int, *, timeout: float = 30.0, interval: float = 0.5
) -> dict:
    """Poll wiring-status until last_applied_fork_version reaches ``version``.

    A save stages reservation.wiring_changed and returns before the NATS consumer
    has necessarily applied it (CI caught exactly this race, PR #623: a
    pre-restore snapshot taken right after a save can still show
    last_applied_fork_version None or behind, then "catch up" on its own between
    two otherwise-adjacent reads, with no restore call responsible for the
    apparent change). Call this to settle onto a known-applied baseline before
    snapshotting wiring-status for a before/after comparison. 30s cap matches the
    other stack-dependent polls in this file (e.g. the standing-reconciler test).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    status: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        status = await _wiring_status(client, reservation_id)
        if status.get("last_applied_fork_version") == version:
            return status
        await asyncio.sleep(interval)
    return status


def _endpoint_pair(delta: dict) -> frozenset:
    return frozenset(
        {(delta["device_a_id"], delta["port_a"]), (delta["device_b_id"], delta["port_b"])}
    )


def _canvas_with_two_edges(device_a_id: str, device_b_id: str, device_c_id: str) -> dict:
    """Three device nodes, two committed L1 edges: A-B (e1) and A-C (e2)."""
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": device_a_id}}},
            {"id": "nB", "data": {"device": {"id": device_b_id}}},
            {"id": "nC", "data": {"device": {"id": device_c_id}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L1", "isProposal": False},
            },
            {
                "id": "e2",
                "source": "nA",
                "target": "nC",
                "data": {"layer": "L1", "isProposal": False},
            },
        ],
    }


async def test_fork_restore_to_draft_is_canvas_only_then_save_reconciles(
    admin_client, fresh_devices
):
    """Restore-to-draft (issue #622, revised after PR #623 review) never wires and
    never appends a fork_versions row of its own.

    GOTCHA fixed post-CI (2026-08-28): fork-on-activation (cabling's create_fork)
    resolves the parent topology's canvas into fork_connections IMMEDIATELY, so a
    fork built on a topology wiring A-B starts with fork_connections ALREADY
    containing A-B, not empty; only the execution-side wiring-status ledger starts
    empty (fork creation stages no reservation.wiring_changed event, only an
    explicit save does; see save_reservation_fork). A first save whose canvas
    omitted the seeded A-B edge would therefore correctly RELEASE it, which is
    exactly what CI caught. This version reads the seeded baseline back explicitly
    via GET .../fork right after activation, and keeps that A-B edge in EVERY
    canvas from then on, so the only "new" wire introduced or removed anywhere in
    this test is the deliberately added A-C edge.

    Sequence: activate onto a topology wiring A-B (fork v1's canvas AND its
    fork_connections both already A-B, confirmed against the GET .../fork read
    right after activation; wiring-status is still empty, since nothing has been
    explicitly saved). Save #1 resubmits the SAME A-B-only canvas: an unchanged
    re-save (v2), released == [] and built == [] since fork_connections already
    held exactly that wire. Save #2 adds the A-C edge alongside A-B (v3): A-B stays
    unchanged, A-C is built. Restoring v1 must then only rewrite the fork's draft
    canvas back to A-B-only and set draft_restored_from_id: it appends NO version
    (the fork's version list stays at exactly v1/v2/v3, proving cabling's own
    latest-fork-version count never advanced, so the standing wiring-heal
    reconciler has nothing to misread as a missed save) and stages NO
    wiring_changed event, so the applied wiring (A-B and A-C, as save #2 left it)
    is untouched; the wiring-status read proves this directly rather than trusting
    the restore response. A follow-up save of the restored draft (A-B-only) is the
    one that actually advances the version (to v4, since restore consumed no
    number) and carries restored_from_id = v1's id; it reconciles for real,
    releasing the A-C edge that only save #2 added and building nothing new (A-B
    was already applied).
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
    canvas_ab_ac = _canvas_with_two_edges(a_id, b_id, c_id)
    topology_id = await _create_topology_with_canvas(admin_client, canvas_ab)
    reservation_id: str | None = None
    try:
        reservation_id = await _create_reservation(admin_client, [a_id, b_id, c_id], topology_id)

        # v1: the activation snapshot. Its canvas AND its fork_connections are
        # already A-B (create_fork resolves the parent canvas into fork_connections
        # immediately; this is the seeded baseline the CI failure exposed). No save
        # has run yet, so the execution-side wiring-status ledger is still empty.
        fork = await admin_client.get(f"/reservations/{reservation_id}/fork")
        assert fork.status_code == 200, fork.text
        fork_body = fork.json()
        assert fork_body["canvas_data"] == canvas_ab
        seeded_connections = fork_body["connections"]
        assert len(seeded_connections) == 1, (
            f"expected fork-on-activation to seed exactly the parent's A-B wire, "
            f"got {seeded_connections}"
        )
        assert _endpoint_pair(seeded_connections[0]) == frozenset({(a_id, "eth1"), (b_id, "eth1")})
        v1 = next(v for v in fork_body["versions"] if v["version_number"] == 1)
        baseline_status = await _wiring_status(admin_client, reservation_id)
        assert baseline_status.get("connections", []) == []

        # Save #1 -> v2: resubmit the SAME A-B-only canvas fork_connections already
        # holds. An unchanged re-save: nothing released, nothing built.
        save_1 = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas_ab},
        )
        assert save_1.status_code == 200, save_1.text
        body_1 = save_1.json()
        assert body_1["version_number"] == 2
        assert body_1["released"] == []
        assert body_1["built"] == []
        assert body_1["unchanged_count"] == 1

        # Save #2 -> v3: add the A-C edge alongside A-B. A-B stays unchanged (it is
        # still in the canvas); A-C is newly built.
        save_2 = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas_ab_ac},
        )
        assert save_2.status_code == 200, save_2.text
        body_2 = save_2.json()
        assert body_2["version_number"] == 3
        assert body_2["released"] == []
        assert body_2["unchanged_count"] == 1
        assert len(body_2["built"]) == 1
        assert _endpoint_pair(body_2["built"][0]) == frozenset({(a_id, "eth2"), (c_id, "eth1")})

        # Snapshot wiring-status AND the fork's version count right before the
        # restore, so both "restore changes nothing" assertions below are real
        # before/after comparisons, not guesses. Settle onto save #2's applied
        # version (3) first: a save stages its wiring_changed event and returns
        # before the NATS consumer has necessarily applied it (CI caught exactly
        # this race, PR #623: an unsettled snapshot can "catch up" on its own
        # between two otherwise-adjacent reads, with no restore call responsible
        # for the apparent change).
        pre_restore_status = await _wait_for_applied_version(admin_client, reservation_id, 3)
        assert pre_restore_status.get("last_applied_fork_version") == 3, (
            f"wiring ledger never settled onto save #2's version before the restore "
            f"snapshot: {pre_restore_status}"
        )
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

        # The draft is back to v1's canvas (A-B only), and the marker is visible
        # on the fork...
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
        # after the restore response matches the pre-restore snapshot exactly (A-B
        # and A-C both still applied, exactly as save #2 left them).
        immediately_after = await _wiring_status(admin_client, reservation_id)
        assert immediately_after == pre_restore_status, (
            "wiring-status changed across the restore call; restore must be canvas-only"
        )

        # Now save the restored draft (A-B only) for real: this is the reconcile
        # the user runs deliberately, and the FIRST save to actually advance the
        # version since the restore (to v4, since restore consumed no number). It
        # must carry restored_from_id = v1's id, release the A-C edge that only
        # save #2 added (A-C is no longer in the canvas), and build nothing new
        # (A-B was already applied from save #1).
        final_save = await admin_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas_ab},
        )
        assert final_save.status_code == 200, final_save.text
        final_body = final_save.json()
        assert final_body["version_number"] == 4

        # Settle onto the final save's applied version (4) before asserting the
        # release: same race as the pre-restore snapshot above, just at the tail
        # end of the flow. released/built below come from the save response
        # itself (a synchronous cabling result, not the async wiring-status
        # surface), so the settle-wait here is about leaving the ledger in a
        # known-settled state rather than about racing this specific assertion.
        settled_after_save = await _wait_for_applied_version(admin_client, reservation_id, 4)
        assert settled_after_save.get("last_applied_fork_version") == 4, (
            f"wiring ledger never settled onto the final save's version: {settled_after_save}"
        )

        assert len(final_body["released"]) == 1
        assert _endpoint_pair(final_body["released"][0]) == frozenset(
            {(a_id, "eth2"), (c_id, "eth1")}
        )
        assert final_body["built"] == []

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


async def test_save_fork_non_member_device_refused_409(
    user_client, admin_client, visible_fresh_device, fresh_devices
):
    """A non-admin owner saving a fork that names a device outside the reservation's
    own membership is refused with 409 fork_device_not_member (D2/D3, the
    2026-09-04 fork endpoint-membership fix). Afterwards the fork's connections are
    unchanged and GET shows no new version: the refused save touched nothing.
    """
    member_device = visible_fresh_device
    foreign_device = (await fresh_devices(1))[0]

    reservation_id = await _create_reservation(user_client, [member_device["id"]], None)
    try:
        # Case A: no parent topology, so the fork is lazy-created on first read with
        # an empty canvas and no wiring.
        got = await user_client.get(f"/reservations/{reservation_id}/fork")
        assert got.status_code == 200, got.text
        before = got.json()
        versions_before = len(before["versions"])
        connections_before = before["connections"]

        canvas = _canvas_with_edge(member_device["id"], foreign_device["id"])
        resp = await user_client.post(
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": canvas},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "fork_device_not_member"
        assert detail["device_ids"] == [foreign_device["id"]]

        # No wiring, no new version: the refused save is a pure no-op.
        after = await user_client.get(f"/reservations/{reservation_id}/fork")
        assert after.status_code == 200, after.text
        after_body = after.json()
        assert len(after_body["versions"]) == versions_before
        assert after_body["connections"] == connections_before
    finally:
        await user_client.delete(f"/reservations/{reservation_id}")
