"""Reservations page search and filters (issue #959), driven in a real browser.

GET /api/reservations/ gained search, status, purpose_category, and a start and end
time window, and ReservationsPage.tsx put Search, Status, Purpose category, and Period
in the shared left filter panel. After every filter action this test captures the
page's OWN list request and replays its query string through the API, then asserts
the rendered rows are exactly the API's answer (same ids, same order, same total). A
filter that renders correctly but sends the wrong request, or the reverse, fails.

Three PENDING reservations are booked as the admin on one device, 250 to 252 days out:
past the bulk test's 200 to 230 days and short of the sort test's 400, because a
cancelled row stays in the list and one starting later than the sort test's pair would
sort above it. Each purpose carries a per-run token, and rows are matched exactly.

The bulk actions are then run on a filtered list: selecting all on a list narrowed to
two of the three rows cancels exactly those two.

Saved state: the page persists the filters in `saved_filters.reservations` and the sort
in `extras["sort:reservations"]`, both per-user server state shared with every other
session on the account. The test reads both baselines, resets them to the defaults,
waits for the debounced preference PATCH that carries each final choice (a late PATCH
after the restore would poison the next run), and restores both in a finally with a
read-back. Cleanup cancels every booked reservation, even on failure.
"""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlparse

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15000
SORT_PREF_KEY = "sort:reservations"
FILTER_PREF_KEY = "reservations"


def _is_list_request(response, expected: dict[str, str]) -> bool:
    """True for the page's own list GET whose filter params are exactly `expected`.

    skip, limit, and the period bounds are not compared here (the bounds carry the
    page's own anchor instant); the replay below uses the full captured query.
    """
    if response.request.method != "GET":
        return False
    parsed = urlparse(response.url)
    if not parsed.path.endswith("/api/reservations/"):
        return False
    params = {k: v for k, v in parse_qsl(parsed.query) if k not in ("skip", "limit")}
    bounds = {k for k in params if k.endswith("_after") or k.endswith("_before")}
    want_bounds = {k for k in expected if k.endswith("_after") or k.endswith("_before")}
    if bounds != want_bounds:
        return False
    plain = {k: v for k, v in params.items() if k not in bounds}
    return plain == {k: v for k, v in expected.items() if k not in want_bounds}


def _prefs_patch_carries(response, key: str, expected) -> bool:
    request = response.request
    if request.method != "PATCH" or "/user-profile/preferences" not in response.url:
        return False
    try:
        body = json.loads(request.post_data or "{}")
    except ValueError:
        return False
    return (body.get("saved_filters") or {}).get(key) == expected


def _read_prefs(page) -> dict:
    return pw_api(page, "GET", "/user-profile/preferences").json()


def _rendered_ids(page) -> list[str]:
    return [t.strip() for t in page.locator("table tbody tr td:nth-child(2)").all_inner_texts()]


def _assert_matches_api(page, response) -> dict:
    """Replay the page's own list query through the API; the table must equal it."""
    query = urlparse(response.url).query
    readback = pw_api(page, "GET", f"/reservations/?{query}").json()
    want = [item["id"][:8] for item in readback["items"]]
    if want:
        expect(page.locator("table tbody tr")).to_have_count(len(want), timeout=WAIT_MS)
        assert _rendered_ids(page) == want
    else:
        expect(page.get_by_text("No reservations match the current filters.")).to_be_visible(
            timeout=WAIT_MS
        )
    expect(page.get_by_text(f"({readback['total']})", exact=True)).to_be_visible(timeout=WAIT_MS)
    return readback


_ACTIONS_LAYOUT = """() => {
  const table = document.querySelector('table');
  const scroller = table.parentElement;
  const card = scroller.closest('.rounded-lg.border');
  // Rendered buttons only: the cell also holds a closed confirm <dialog>.
  const buttons = [
    ...table.querySelectorAll('tbody tr:first-child td:last-child button'),
  ].filter((b) => b.offsetParent !== null);
  const last = buttons[buttons.length - 1];
  return {
    cardRight: card.getBoundingClientRect().right,
    buttonRight: last ? last.getBoundingClientRect().right : null,
    scrollWidth: scroller.scrollWidth,
    clientWidth: scroller.clientWidth,
  };
}"""


def _assert_actions_inside_card(page) -> None:
    """At 1280x720, with the filter panel beside the table, the first row's last action
    button sits inside the table card and the table does not scroll sideways."""
    m = page.evaluate(_ACTIONS_LAYOUT)
    assert m["buttonRight"] is not None and m["buttonRight"] > 0, m
    assert m["buttonRight"] <= m["cardRight"], m
    assert m["scrollWidth"] <= m["clientWidth"], m


def _status(page, reservation: dict) -> str:
    return pw_api(page, "GET", f"/reservations/{reservation['id']}").json()["status"]


def _wait_status(page, reservation: dict, want: str, timeout_s: float = 30) -> str:
    deadline = time.monotonic() + timeout_s
    status = _status(page, reservation)
    while status != want and time.monotonic() < deadline:
        time.sleep(1)
        status = _status(page, reservation)
    return status


def _set_prefs(page, filter_value, sort_value) -> None:
    resp = pw_api(
        page,
        "PATCH",
        "/user-profile/preferences",
        json={
            "saved_filters": {FILTER_PREF_KEY: filter_value},
            "extras": {SORT_PREF_KEY: sort_value},
        },
        allow_errors=True,
    )
    log_cleanup_failure("reservations preferences", FILTER_PREF_KEY, resp)


@pytest.fixture
def filter_trio(pw_page):
    """Three PENDING reservations on one device; purposes share a per-run token."""
    pw_login(pw_page)
    resp = pw_api(pw_page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True)
    if resp.status_code != 200:
        pytest.skip(f"cannot list devices: {resp.status_code}")
    payload = resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        pytest.skip("no available exclusive device to reserve")
    device_id = available[0]["id"]
    token = f"e2e959{uuid.uuid4().hex[:8]}"
    base = datetime.now(timezone.utc) + timedelta(days=250)
    created: list[dict] = []
    try:
        for n, label in enumerate(("keep", "drop one", "drop two")):
            start = base + timedelta(days=n)
            body = {
                "device_ids": [device_id],
                "purpose": f"{token} {label}",
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=30)).isoformat(),
            }
            r = pw_api(pw_page, "POST", "/reservations/", json=body, allow_errors=True)
            if r.status_code != 201:
                pytest.skip(f"reservation create failed: {r.status_code} {r.text}")
            created.append(r.json())
        yield token, created
    finally:
        for reservation in created:
            r = pw_api(pw_page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True)
            log_cleanup_failure("reservation", reservation["id"], r)


def test_search_and_status_filters_and_bulk_on_a_filtered_list(pw_page, filter_trio):
    token, (keep, drop_one, drop_two) = filter_trio
    page = pw_page

    prefs = _read_prefs(page)
    baseline_filter = (prefs.get("saved_filters") or {}).get(FILTER_PREF_KEY) or {}
    baseline_sort = (prefs.get("extras") or {}).get(SORT_PREF_KEY)
    _set_prefs(page, {"search": ""}, None)
    try:
        page.set_viewport_size({"width": 1280, "height": 720})
        page.goto(f"{HOST_BASE_URL}/reservations")
        panel = page.get_by_role("region", name="Filters")
        search = panel.get_by_label("Search reservations", exact=True)
        status = panel.get_by_label("Status", exact=True)
        period = panel.get_by_label("Period", exact=True)
        expect(search).to_be_visible(timeout=WAIT_MS)

        # Search narrows to this run's three rows (newest created first).
        with page.expect_response(
            lambda r: _prefs_patch_carries(r, FILTER_PREF_KEY, {"search": token}),
            timeout=WAIT_MS,
        ):
            with page.expect_response(
                lambda r: _is_list_request(r, {"search": token}), timeout=WAIT_MS
            ) as listed:
                search.fill(token)
        readback = _assert_matches_api(page, listed.value)
        assert {i["id"] for i in readback["items"]} == {keep["id"], drop_one["id"], drop_two["id"]}
        _assert_actions_inside_card(page)

        # The Upcoming period composes with the search: all three start 250+ days out.
        with page.expect_response(
            lambda r: _is_list_request(r, {"search": token, "starts_after": "x"}),
            timeout=WAIT_MS,
        ) as listed:
            period.select_option("upcoming")
        readback = _assert_matches_api(page, listed.value)
        assert readback["total"] == 3
        # Back to All: a view already in the query cache may not refetch, so the
        # next step's own request is what proves the period was dropped.
        period.select_option("")

        # A narrower search: the two "drop" rows only.
        drop_term = f"{token} drop"
        with page.expect_response(
            lambda r: _is_list_request(r, {"search": drop_term}),
            timeout=WAIT_MS,
        ) as listed:
            search.fill(drop_term)
        readback = _assert_matches_api(page, listed.value)
        assert {i["id"] for i in readback["items"]} == {drop_one["id"], drop_two["id"]}

        # Bulk on the filtered list: select all takes exactly the two listed rows.
        page.get_by_role("checkbox", name="Select all reservations on this page").check()
        bar = page.get_by_role("status", name="Selection")
        expect(bar).to_contain_text("2 selected")
        page.get_by_role("button", name="Cancel selected", exact=True).click()
        dialog = page.locator("dialog[open]")
        expect(dialog).to_contain_text("Cancel 2 reservations?")
        dialog.get_by_role("button", name="Cancel 2 reservations", exact=True).click()
        page.mouse.move(5, 400)
        assert _wait_status(page, drop_one, "CANCELLED") == "CANCELLED"
        assert _wait_status(page, drop_two, "CANCELLED") == "CANCELLED"
        assert _status(page, keep) == "PENDING"

        # Status filter on the run's rows: Cancelled lists the two, Pending the one.
        with page.expect_response(
            lambda r: _is_list_request(r, {"search": token}), timeout=WAIT_MS
        ):
            search.fill(token)
        with page.expect_response(
            lambda r: _prefs_patch_carries(
                r, FILTER_PREF_KEY, {"search": token, "status": "CANCELLED"}
            ),
            timeout=WAIT_MS,
        ):
            with page.expect_response(
                lambda r: _is_list_request(r, {"search": token, "status": "CANCELLED"}),
                timeout=WAIT_MS,
            ) as listed:
                status.select_option("CANCELLED")
        readback = _assert_matches_api(page, listed.value)
        assert {i["id"] for i in readback["items"]} == {drop_one["id"], drop_two["id"]}
        assert all(i["status"] == "CANCELLED" for i in readback["items"])

        with page.expect_response(
            lambda r: _prefs_patch_carries(
                r, FILTER_PREF_KEY, {"search": token, "status": "PENDING"}
            ),
            timeout=WAIT_MS,
        ):
            with page.expect_response(
                lambda r: _is_list_request(r, {"search": token, "status": "PENDING"}),
                timeout=WAIT_MS,
            ) as listed:
                status.select_option("PENDING")
        readback = _assert_matches_api(page, listed.value)
        assert [i["id"] for i in readback["items"]] == [keep["id"]]

        # The choice persists across a reload.
        page.reload()
        expect(status).to_have_value("PENDING", timeout=WAIT_MS)
        expect(search).to_have_value(token)

        # Filtered-empty state, then its Clear control resets every filter.
        with page.expect_response(
            lambda r: _is_list_request(r, {"search": token, "status": "FAILED"}),
            timeout=WAIT_MS,
        ) as listed:
            status.select_option("FAILED")
        _assert_matches_api(page, listed.value)
        empty = page.get_by_text("No reservations match the current filters.")
        with page.expect_response(
            lambda r: _prefs_patch_carries(r, FILTER_PREF_KEY, {"search": ""}),
            timeout=WAIT_MS,
        ):
            empty.get_by_role("button", name="Clear filters", exact=True).click()
        expect(status).to_have_value("")
        expect(search).to_have_value("")
        # The unfiltered view may come from the query cache without a request, and
        # the unfiltered total moves whenever anything else on the stack creates a
        # reservation, so a separate API read can disagree with the page. Reload and
        # compare the table to the body of the list response the page itself got.
        with page.expect_response(lambda r: _is_list_request(r, {}), timeout=WAIT_MS) as listed:
            page.reload()
        unfiltered = listed.value.json()
        expect(page.locator("table tbody tr")).to_have_count(
            len(unfiltered["items"]), timeout=WAIT_MS
        )
        assert _rendered_ids(page) == [item["id"][:8] for item in unfiltered["items"]]
        expect(page.get_by_text(f"({unfiltered['total']})", exact=True)).to_be_visible(
            timeout=WAIT_MS
        )
        expect(status).to_have_value("")
        expect(search).to_have_value("")
        assert (_read_prefs(page).get("saved_filters") or {}).get(FILTER_PREF_KEY) == {"search": ""}
    finally:
        _set_prefs(page, baseline_filter, baseline_sort)
        restored = pw_api(page, "GET", "/user-profile/preferences", allow_errors=True)
        if restored.status_code == 200:
            body = restored.json()
            got_filter = (body.get("saved_filters") or {}).get(FILTER_PREF_KEY) or {}
            got_sort = (body.get("extras") or {}).get(SORT_PREF_KEY)
            if got_filter != baseline_filter or got_sort != baseline_sort:
                pytest.fail(
                    "reservations preferences not restored: "
                    f"{got_filter!r}/{got_sort!r} != {baseline_filter!r}/{baseline_sort!r}"
                )
