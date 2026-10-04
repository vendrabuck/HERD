"""Playwright e2e tests for toast placement (issue #942).

Toasts render at the bottom centre (`<Toaster position="bottom-center" />` in
frontend/src/App.tsx). At the old top-right position a toast covered the
topology editor's Save button, and because react-hot-toast pauses a toast's
timer while the pointer is over it, the pointer resting on Save after a click
kept the toast up and Save unclickable.

The invariant pinned here, at 1280x720 and 1920x1080, with one toast and with
a stack of three: no toast covers the centre point of any visible control on
the topology editor (its toolbar included) or on the inventory list scrolled to
the bottom (row actions, the filter panel, Pagination and its "Rows per page"
selector). jsdom cannot see overlap, so this measures real boxes.

Every toast comes from the page's own code path. The single toast on the editor
is a real save of a throwaway topology. The stacks of three, and every toast on
the inventory list, come from a request the test answers with a 500 through
`page.route`, so the page raises its own error toast and nothing on the shared
stack changes; the inventory test also deletes any duplicate that slipped
through, and preference writes are refused at the network layer so no saved
filter or page size is touched.

Needs: the editor test creates and deletes its own empty topology and needs no
seed. The inventory test needs at least one device in inventory and skips on
an empty one (the pre-seed e2e pass); the seeded pass always has devices.
"""

import uuid

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15_000
VIEWPORTS = ((1280, 720), (1920, 1080))
TOAST_BARS = "[data-rht-toaster] [role='status']"

# Every visible control centre that lies inside a toast's box, as
# [{name, x, y, w, h}]. The toast box is the bar around the role=status
# message; the toaster container itself spans the viewport and takes no
# pointer events, so only the bars can cover anything.
_BLOCKED_CONTROLS_JS = """
() => {
  const toaster = document.querySelector('[data-rht-toaster]');
  const bars = [...document.querySelectorAll("[data-rht-toaster] [role='status']")]
    .map(s => s.parentElement.getBoundingClientRect())
    .filter(r => r.width > 0 && r.height > 0);
  const sel = 'button, a[href], input:not([type=hidden]), select, textarea, '
    + '[role=checkbox], [role=combobox], [role=tab], summary';
  const vw = window.innerWidth, vh = window.innerHeight;
  const blocked = [];
  let checked = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (toaster && toaster.contains(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx > vw || cy > vh) continue;
    checked++;
    if (bars.some(b => cx >= b.left && cx <= b.right && cy >= b.top && cy <= b.bottom)) {
      const name = (el.getAttribute('aria-label') || el.getAttribute('title')
        || el.textContent || el.tagName).trim().slice(0, 40);
      blocked.push({name, x: Math.round(r.x), y: Math.round(r.y),
        w: Math.round(r.width), h: Math.round(r.height)});
    }
  }
  return {bars: bars.length, checked, blocked};
}
"""

# Scroll every scrollable box to its end, so controls at the bottom of a long
# list (Pagination, the last row) sit where a bottom-centre toast appears.
_SCROLL_TO_END_JS = """
() => {
  for (const el of [document.scrollingElement, ...document.querySelectorAll('*')]) {
    if (el && el.scrollHeight > el.clientHeight + 1) el.scrollTop = el.scrollHeight;
  }
}
"""


def _assert_no_control_covered(page, *, toasts: int, where: str) -> None:
    expect(page.locator(TOAST_BARS)).to_have_count(toasts, timeout=WAIT_MS)
    result = page.evaluate(_BLOCKED_CONTROLS_JS)
    assert result["bars"] == toasts, result
    # Guard against a vacuous pass: the page must actually have rendered controls.
    assert result["checked"] >= 5, f"{where}: only {result['checked']} controls on screen"
    assert result["blocked"] == [], f"{where}: toast covers {result['blocked']}"


def _park_on_toast(page) -> None:
    """Rest the pointer on the newest toast so the stack holds while it is measured.

    Hover-to-pause is react-hot-toast's own behaviour and stays (issue #942);
    using it here makes the stack of three deterministic on a slow stack.
    """
    box = page.locator(TOAST_BARS).last.bounding_box()
    assert box, "toast bar has no box"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _park_off_toaster(page) -> None:
    # Left edge: a toast is at most 350 px wide and centred, so x = 5 is
    # outside every toast at any e2e viewport.
    page.mouse.move(5, 400)


def _drain_toasts(page) -> None:
    _park_off_toaster(page)
    expect(page.locator(TOAST_BARS)).to_have_count(0, timeout=WAIT_MS)


@pytest.fixture
def empty_topology(pw_page):
    pw_login(pw_page)
    topo = pw_api(
        pw_page,
        "POST",
        "/cabling/topologies",
        json={"name": f"pw-toast-placement-{uuid.uuid4().hex[:8]}"},
    ).json()
    try:
        yield topo
    finally:
        resp = pw_api(pw_page, "DELETE", f"/cabling/topologies/{topo['id']}", allow_errors=True)
        if resp.status_code != 404:
            log_cleanup_failure("topology", topo["id"], resp)


def test_editor_toasts_cover_no_control_and_save_stays_clickable(pw_page, empty_topology):
    tid = empty_topology["id"]
    put_path = f"/api/cabling/topologies/{tid}"
    save = pw_page.get_by_role("button", name="Save", exact=True)

    for width, height in VIEWPORTS:
        where = f"editor {width}x{height}"
        pw_page.set_viewport_size({"width": width, "height": height})
        pw_page.goto(f"{HOST_BASE_URL}/topology/{tid}")
        expect(save).to_be_visible(timeout=WAIT_MS)

        # One real toast: a real save of the throwaway topology.
        with pw_page.expect_response(
            lambda r: r.request.method == "PUT" and r.url.endswith(put_path), timeout=WAIT_MS
        ) as first:
            save.click()
        assert first.value.status == 200
        expect(pw_page.get_by_text("Topology saved", exact=True)).to_be_visible(timeout=WAIT_MS)
        _assert_no_control_covered(pw_page, toasts=1, where=f"{where}, one toast")

        # Save is clickable while that toast is up (a covered button would
        # fail the click's hit-target check), and the pointer left resting on
        # Save does not hold the new toast: the stack drains on its own.
        with pw_page.expect_response(
            lambda r: r.request.method == "PUT" and r.url.endswith(put_path), timeout=WAIT_MS
        ) as second:
            save.click(timeout=5_000)
        assert second.value.status == 200
        expect(pw_page.locator(TOAST_BARS)).to_have_count(0, timeout=WAIT_MS)

        # A stack of three error toasts: the page's own save-failure toast,
        # with the PUT answered 500 so the topology is not written.
        def fail_put(route):
            if route.request.method == "PUT":
                route.fulfill(status=500, json={"detail": "injected by the #942 e2e test"})
            else:
                route.continue_()

        pw_page.route(f"**{put_path}", fail_put)
        try:
            for _ in range(3):
                save.click(timeout=5_000)
                expect(save).to_be_enabled(timeout=WAIT_MS)
            _park_on_toast(pw_page)
            _assert_no_control_covered(pw_page, toasts=3, where=f"{where}, stack of three")
        finally:
            pw_page.unroute(f"**{put_path}", fail_put)
        _drain_toasts(pw_page)


def _refuse_preference_writes(route):
    if route.request.method == "GET":
        route.continue_()
    else:
        route.abort()


def test_inventory_list_toasts_cover_no_control_at_the_bottom(pw_page):
    pw_login(pw_page)
    before = pw_api(pw_page, "GET", "/inventory/devices", params={"limit": 1}).json()
    if before["total"] == 0:
        pytest.skip("inventory has no devices to list (pre-seed pass)")

    # Nothing this test does may persist a saved filter or page size.
    pw_page.route("**/api/user-profile/preferences**", _refuse_preference_writes)

    # Duplicate ids that existed before the test, per copy name, so cleanup
    # can only ever remove a copy this test made.
    copies: dict[str, set[str]] = {}

    def copies_named(name: str) -> list[dict]:
        found = pw_api(
            pw_page, "GET", "/inventory/devices", params={"search": name, "limit": 100}
        ).json()["items"]
        return [d for d in found if d["name"] == name]

    def fail_create(route):
        if route.request.method == "POST":
            route.fulfill(status=500, json={"detail": "injected by the #942 e2e test"})
        else:
            route.continue_()

    pw_page.route("**/api/inventory/devices", fail_create)
    try:
        for width, height in VIEWPORTS:
            where = f"inventory {width}x{height}"
            pw_page.set_viewport_size({"width": width, "height": height})
            pw_page.goto(f"{HOST_BASE_URL}/inventory")
            duplicate = pw_page.get_by_role("button", name="Duplicate device")
            expect(duplicate.first).to_be_visible(timeout=WAIT_MS)
            expect(pw_page.get_by_label("Rows per page")).to_be_visible()
            target = duplicate.last
            name_cell = target.locator("xpath=ancestor::tr").locator("td.font-medium")
            copy_name = f"Copy of {name_cell.inner_text(timeout=WAIT_MS).strip()}"
            if copy_name not in copies:
                copies[copy_name] = {d["id"] for d in copies_named(copy_name)}
            pw_page.evaluate(_SCROLL_TO_END_JS)

            # The page's own duplicate-failure toast, once and then three deep.
            for count in (1, 3):
                for _ in range(count):
                    target.click(timeout=5_000)
                    pw_page.wait_for_timeout(150)
                expect(pw_page.locator(TOAST_BARS)).to_have_count(count, timeout=WAIT_MS)
                _park_on_toast(pw_page)
                pw_page.evaluate(_SCROLL_TO_END_JS)
                expect(pw_page.get_by_label("Rows per page")).to_be_in_viewport()
                _assert_no_control_covered(
                    pw_page, toasts=count, where=f"{where}, {count} toast(s), scrolled to end"
                )
                _drain_toasts(pw_page)
    finally:
        pw_page.unroute("**/api/inventory/devices", fail_create)
        # The POST was answered 500, so no copy should exist; delete any that
        # slipped through anyway, so the shared stack never keeps one.
        for name, existing in copies.items():
            for device in copies_named(name):
                if device["id"] not in existing:
                    resp = pw_api(
                        pw_page, "DELETE", f"/inventory/devices/{device['id']}", allow_errors=True
                    )
                    log_cleanup_failure("device", device["id"], resp)
