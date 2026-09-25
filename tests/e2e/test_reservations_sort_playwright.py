"""Playwright e2e test for reservations list sorting (issue #844).

GET /api/reservations/ gained sort_by/sort_dir query params
(services/reservations/app/routers/reservations.py) and ReservationsPage.tsx
turned the Owner/Status/Period/Purpose column headings into sort controls
(frontend/src/pages/ReservationsPage.tsx). This asserts the effect through
backend API read-back, not UI acknowledgment, per the standing convention in
test_connections_playwright.py: after sorting the table by start time
descending in the UI, the first RENDERED row must match the first item a
direct API call with the identical query params returns.

Two reservations are created on the same available device, a year or more
out and clearly ordered in start_time, so the sort has something
unambiguous to prove regardless of whatever else is on the shared stack.
Sorting itself is read-only (GET requests only), so nothing about the sort
choice needs restoring afterward; only the reservations this test creates
do, and cleanup runs even on failure.
"""

from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15000


@pytest.fixture
def pw_two_future_reservations(pw_page):
    """Two non-overlapping reservations on one device, far apart in start_time.

    Mirrors the device-selection half of conftest.py's transient_reservation
    (first AVAILABLE, exclusive, dut_only device): only device availability
    matters here, not ports or cabling, so no fresh-device creation is
    needed the way test_connections_bulk_playwright.py's bulk_fixture_devices
    does for port-level isolation. Both reservations sit more than a year
    out, far past anything else on the shared stack (transient_reservation
    and its Playwright analogues all book at "now"), so no other reservation
    should plausibly sort ahead of them by start_time.
    """
    pw_login(pw_page)

    devices_resp = pw_api(
        pw_page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True
    )
    if devices_resp.status_code != 200:
        pytest.skip(f"cannot list devices: {devices_resp.status_code}")
    payload = devices_resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        pytest.skip("no available exclusive device to reserve")
    device_id = available[0]["id"]

    base = datetime.now(timezone.utc) + timedelta(days=400)
    created = []
    try:
        for offset_days, purpose in ((0, "e2e sort earlier"), (10, "e2e sort later")):
            start = base + timedelta(days=offset_days)
            body = {
                "device_ids": [device_id],
                "purpose": purpose,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=30)).isoformat(),
            }
            resp = pw_api(pw_page, "POST", "/reservations/", json=body, allow_errors=True)
            if resp.status_code != 201:
                pytest.skip(f"reservation create failed: {resp.status_code} {resp.text}")
            created.append(resp.json())

        # created[1] (offset_days=10) starts later, so it must sort first
        # under start_time desc; created[0] is the earlier of the two.
        yield created[1], created[0]
    finally:
        for reservation in created:
            resp = pw_api(
                pw_page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True
            )
            log_cleanup_failure("reservation", reservation["id"], resp)


def test_sort_by_start_time_desc_matches_api_readback(pw_page, pw_two_future_reservations):
    """Sorting the Period column descending puts the API's own first row on top."""
    later, _earlier = pw_two_future_reservations

    pw_page.goto(f"{HOST_BASE_URL}/reservations")
    period_heading = pw_page.get_by_role("button", name="Period")
    expect(period_heading).to_be_visible(timeout=WAIT_MS)

    # First click: ascending. Second click: descending. Today's default is
    # created_at desc, so two clicks are needed to reach start_time desc.
    period_heading.click()
    period_heading.click()

    period_header_cell = pw_page.get_by_role("columnheader", name="Period")
    expect(period_header_cell).to_have_attribute("aria-sort", "descending", timeout=WAIT_MS)

    # Wait for the fixture's later reservation to actually be rendered,
    # proving the sorted request has round-tripped, before reading the
    # table's first data row.
    expect(pw_page.get_by_text(later["purpose"])).to_be_visible(timeout=WAIT_MS)

    first_row = pw_page.locator("table tbody tr").first
    expect(first_row).to_contain_text(later["id"][:8])

    # Effect assertion: read the identical query back through the API
    # directly, rather than trusting the UI's own claim that it applied the
    # sort. Same params the page itself sends (no `all`: this admin's own
    # reservations, matching the default "My Reservations" view).
    readback = pw_api(
        pw_page,
        "GET",
        "/reservations/",
        params={"sort_by": "start_time", "sort_dir": "desc", "limit": 50},
    ).json()
    assert readback["items"], "expected at least the two fixture reservations back"
    assert readback["items"][0]["id"] == later["id"]
