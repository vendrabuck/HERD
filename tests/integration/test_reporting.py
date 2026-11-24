"""Integration tests for the utilization report endpoints against a running HERD stack."""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _create_short_reservation(client, device_id: str, hours: int = 1) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "reporting integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=hours)).isoformat(),
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


# Each offset with the UTC hours at which a start written in it has a local date that
# is not its UTC date: -12:00 puts it on the day before while the UTC hour is under 12,
# +14:00 on the day after from 10:00 UTC on. Both are real zones.
_OFFSET_WINDOWS = ((timedelta(hours=-12), range(0, 12)), (timedelta(hours=14), range(10, 24)))


def _next_start_in_utc_hours(after: datetime, utc_hours: range) -> datetime:
    """The first instant at least 10 minutes after `after` whose UTC hour is in `utc_hours`.

    It steps to 10 minutes past the next hour until the hour fits, so the answer is
    always less than 24 hours and 10 minutes after `after`.
    """
    candidate = after + timedelta(minutes=10)
    while candidate.hour not in utc_hours:
        candidate = candidate.replace(minute=10, second=0, microsecond=0) + timedelta(hours=1)
    return candidate


async def test_utilization_report_by_day_sums_to_total_with_an_offset_start(
    admin_client, fresh_device
):
    """Issue #1523, OPS-REPORT-4: a start whose local date is not its UTC date.

    One window per offset, -12:00 and +14:00, on every run whatever the hour: each
    starts at the next instant within a day of now whose local date differs from its
    UTC date, and lasts an hour. The reservation runs 26 hours from now, so it covers
    both windows and is clipped to each window's start, the bound the day split used
    to read local date fields from. Other tests' ACTIVE rows may share a window; the
    invariant holds for all of them.
    """
    reservation = await _create_short_reservation(admin_client, fresh_device["id"], hours=26)
    try:
        now = datetime.now(timezone.utc)
        for offset, utc_hours in _OFFSET_WINDOWS:
            start_utc = _next_start_in_utc_hours(now, utc_hours)
            end_utc = start_utc + timedelta(hours=1)
            start = start_utc.astimezone(timezone(offset))
            assert start.date() != start_utc.date(), start.isoformat()
            resp = await admin_client.get(
                "/reservations/reports/utilization",
                params={"start": start.isoformat(), "end": end_utc.isoformat(), "status": "ACTIVE"},
            )
            assert resp.status_code == 200, (start.isoformat(), resp.text)
            data = resp.json()
            echoed = datetime.fromisoformat(data["window_start"].replace("Z", "+00:00"))
            assert echoed == start_utc
            assert echoed.utcoffset() == timedelta(0)
            # This test's reservation covers the whole hour.
            assert data["total_hours"] >= 1.0 - 1e-4, (start.isoformat(), data["total_hours"])
            utc_days = {start_utc.date().isoformat(), end_utc.date().isoformat()}
            assert {b["day"] for b in data["by_day"]} <= utc_days, start.isoformat()
            assert all(b["hours"] > 0 for b in data["by_day"])
            # Each bucket and the total are rounded to four places on their own.
            assert sum(b["hours"] for b in data["by_day"]) == pytest.approx(
                data["total_hours"], abs=1e-4 * (len(data["by_day"]) + 1)
            ), start.isoformat()
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
