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
out and clearly ordered in start_time, so the sort has something to prove
that does not depend on the stack's other rows. Other far-future rows (a
cancelled row stays in the list, and any other test or user may have booked
later than these) can sort ahead of the pair, so the test never assumes its
own reservation is first: it asserts that the first rendered row is the first
item the API returns for the identical query, and that the API orders the
pair later-before-earlier.

Rerunnable on a reused stack (issue #951). Two things make that true. The
reservations are cleaned up by cancelling, and a cancelled row stays in the
list, so each run's purposes carry a unique suffix and are matched exactly.
And the page PERSISTS the sort choice per user (extras["sort:reservations"]
in the user-profile preferences, issue #844), so the test resets that
preference to the default before it clicks, waits for the debounced
preference PATCH its clicks cause, and restores the admin's baseline
afterward; cleanup runs even on failure.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15000
# The preferences key ReservationsPage persists its sort under
# (sortExtraKey("reservations") in frontend/src/stores/preferencesStore.ts).
SORT_PREF_KEY = "sort:reservations"


@pytest.fixture
def pw_two_future_reservations(pw_page):
    """Two non-overlapping reservations on one device, far apart in start_time.

    Mirrors the device-selection half of conftest.py's transient_reservation
    (first AVAILABLE, exclusive, dut_only device): only device availability
    matters here, not ports or cabling, so no fresh-device creation is
    needed the way test_connections_bulk_playwright.py's bulk_fixture_devices
    does for port-level isolation. Both reservations sit more than a year
    out and clearly ordered against each other. Other rows may still sort
    ahead of the pair on a reused stack, so the test asserts only their
    relative order and the first-row match, never that either is first.
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
        run = uuid.uuid4().hex[:8]
        for offset_days, purpose in (
            (0, f"e2e sort earlier {run}"),
            (10, f"e2e sort later {run}"),
        ):
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


def _set_sort_preference(page, value) -> None:
    """Write the admin's saved Reservations sort (None clears it to the default)."""
    resp = pw_api(
        page,
        "PATCH",
        "/user-profile/preferences",
        json={"extras": {SORT_PREF_KEY: value}},
        allow_errors=True,
    )
    log_cleanup_failure("sort preference", SORT_PREF_KEY, resp)


def _is_sort_desc_patch(response) -> bool:
    request = response.request
    if request.method != "PATCH" or "/user-profile/preferences" not in response.url:
        return False
    sort = ((request.post_data_json or {}).get("extras") or {}).get(SORT_PREF_KEY) or {}
    return sort.get("sortDir") == "desc"


def test_sort_by_start_time_desc_matches_api_readback(pw_page, pw_two_future_reservations):
    """Sorting the Period column descending renders the API's own order."""
    later, earlier = pw_two_future_reservations

    # The sort choice is a saved preference: remember the admin's, then reset it
    # to the default so the two-click path below starts from a known state.
    prefs = pw_api(pw_page, "GET", "/user-profile/preferences").json()
    baseline_sort = (prefs.get("extras") or {}).get(SORT_PREF_KEY)
    _set_sort_preference(pw_page, None)
    try:
        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        period_heading = pw_page.get_by_role("button", name="Period")
        expect(period_heading).to_be_visible(timeout=WAIT_MS)

        # First click: ascending. Second click: descending. The default is
        # created_at desc, so two clicks are needed to reach start_time desc.
        # The page saves the choice as a debounced preference PATCH; wait for
        # the one that carries the final choice, or it lands after the restore
        # below and poisons the next run.
        period_heading.click()
        with pw_page.expect_response(_is_sort_desc_patch, timeout=WAIT_MS):
            period_heading.click()

        period_header_cell = pw_page.get_by_role("columnheader", name="Period")
        expect(period_header_cell).to_have_attribute("aria-sort", "descending", timeout=WAIT_MS)

        # Effect assertion: read the identical query back through the API
        # directly, rather than trusting the UI's own claim that it applied the
        # sort. Same params the page itself sends (no `all`: this admin's own
        # reservations, matching the default "My Reservations" view), at the
        # endpoint's maximum limit so a crowded stack cannot hide the pair.
        readback = pw_api(
            pw_page,
            "GET",
            "/reservations/",
            params={"sort_by": "start_time", "sort_dir": "desc", "limit": 500},
        ).json()
        items = readback["items"]
        assert items, "expected at least the two fixture reservations back"
        order = [r["id"] for r in items]
        assert later["id"] in order and earlier["id"] in order, (
            "both fixture reservations must be in the read-back "
            f"(total={readback.get('total')}, returned={len(order)})"
        )
        assert order.index(later["id"]) < order.index(earlier["id"])

        # The first rendered row is the API's first item, whichever reservation
        # that is. Wait for the sorted request to land first: the table shows
        # the API's first row once it does.
        first_row = pw_page.locator("table tbody tr").first
        expect(first_row).to_contain_text(items[0]["id"][:8], timeout=WAIT_MS)

        # The choice really was saved, which is why it has to be restored.
        saved = pw_api(pw_page, "GET", "/user-profile/preferences").json()
        assert (saved.get("extras") or {}).get(SORT_PREF_KEY) == {
            "sortBy": "start_time",
            "sortDir": "desc",
        }
    finally:
        _set_sort_preference(pw_page, baseline_sort)
        restored = pw_api(pw_page, "GET", "/user-profile/preferences", allow_errors=True)
        if restored.status_code == 200:
            assert (restored.json().get("extras") or {}).get(SORT_PREF_KEY) == baseline_sort
