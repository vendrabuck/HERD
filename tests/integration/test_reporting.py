"""Integration tests for the utilization report endpoints against a running HERD stack."""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _create_short_reservation(client, device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "reporting integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    resp = await client.post("/reservations/", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_utilization_report_reflects_reservation(admin_client, fresh_device):
    reservation = await _create_short_reservation(admin_client, fresh_device["id"])
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
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_reservations"] >= 1
        device_ids = {b["device_id"] for b in data["by_device"]}
        assert fresh_device["id"] in device_ids
        # by_topology_type populated from the Reservation model
        topology_types = {b["topology_type"] for b in data["by_topology_type"]}
        assert "PHYSICAL" in topology_types
        # execution_run_count is populated from the execution service; may be 0
        # but must be a non-null integer once the service is reachable with auth.
        assert isinstance(data["execution_run_count"], int)
        # by_day covers at least one calendar day in the window.
        assert len(data["by_day"]) >= 1
        assert all("day" in b and "hours" in b for b in data["by_day"])
        # by_group rolls up the admin's group membership (at least one bucket
        # must exist once the report has any users).
        assert len(data["by_group"]) >= 1
        assert all("group_name" in b and "hours" in b for b in data["by_group"])
    finally:
        await admin_client.delete(f"/reservations/{reservation['id']}")


async def test_utilization_report_non_admin_forbidden(user_client):
    now = datetime.now(timezone.utc)
    resp = await user_client.get(
        "/reservations/reports/utilization",
        params={
            "start": (now - timedelta(days=1)).isoformat(),
            "end": now.isoformat(),
        },
    )
    assert resp.status_code == 403


async def test_utilization_report_csv_download(admin_client, fresh_device):
    reservation = await _create_short_reservation(admin_client, fresh_device["id"])
    try:
        now = datetime.now(timezone.utc)
        params = {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
            "status": "ACTIVE",
        }

        user_resp = await admin_client.get(
            "/reservations/reports/utilization.csv",
            params={**params, "section": "user"},
        )
        assert user_resp.status_code == 200
        assert user_resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in user_resp.headers["content-disposition"]
        assert user_resp.text.splitlines()[0] == "user_id,owner_name,hours,reservation_count"

        device_resp = await admin_client.get(
            "/reservations/reports/utilization.csv",
            params={**params, "section": "device"},
        )
        assert device_resp.status_code == 200
        assert (
            device_resp.text.splitlines()[0]
            == "device_id,hours,reservation_count,transit_reservations,transit_hours"
        )
        assert fresh_device["id"] in device_resp.text
    finally:
        await admin_client.delete(f"/reservations/{reservation['id']}")


async def test_fleet_section_counts_active_and_lists_idle_devices(admin_client, fresh_device):
    reservation = await _create_short_reservation(admin_client, fresh_device["id"])
    try:
        now = datetime.now(timezone.utc)
        resp = await admin_client.get(
            "/reservations/reports/utilization",
            params={
                "start": (now - timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                # No status param: legacy sections default to COMPLETED,
                # the fleet section defaults to ACTIVE+COMPLETED.
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        fleet = data["fleet"]
        assert fleet is not None, "fleet must be populated when inventory is reachable"
        assert fleet["device_count"] >= 1
        assert fleet["window_hours"] == pytest.approx(3.0, abs=0.01)

        by_id = {d["device_id"]: d for d in fleet["devices"]}
        assert fresh_device["id"] in by_id
        mine = by_id[fresh_device["id"]]
        # The ACTIVE reservation counts toward fleet utilization...
        assert mine["hours"] > 0
        assert mine["utilization_pct"] > 0
        assert mine["reservation_count"] >= 1
        assert mine["name"] == fresh_device["name"]
        # ...but stays invisible to the legacy COMPLETED-only default.
        legacy_ids = {b["device_id"] for b in data["by_device"]}
        assert fresh_device["id"] not in legacy_ids

        # Idle accounting is consistent with the device rows.
        idle_rows = [d for d in fleet["devices"] if d["reservation_count"] == 0]
        assert fleet["idle_device_count"] == len(idle_rows)
    finally:
        await admin_client.delete(f"/reservations/{reservation['id']}")


async def test_utilization_report_csv_fleet_section(admin_client, fresh_device):
    reservation = await _create_short_reservation(admin_client, fresh_device["id"])
    try:
        now = datetime.now(timezone.utc)
        resp = await admin_client.get(
            "/reservations/reports/utilization.csv",
            params={
                "start": (now - timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                "section": "fleet",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        lines = resp.text.splitlines()
        assert lines[0] == "device_id,name,status,hours,utilization_pct,reservation_count"
        assert fresh_device["id"] in resp.text
    finally:
        await admin_client.delete(f"/reservations/{reservation['id']}")


async def test_utilization_report_csv_rejects_unknown_section(admin_client):
    now = datetime.now(timezone.utc)
    resp = await admin_client.get(
        "/reservations/reports/utilization.csv",
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": now.isoformat(),
            "section": "template",
        },
    )
    assert resp.status_code == 422
