"""E2E tests for the reservation-fork live-edit flow (ADR 0006 Decision 6).

These cover the epic's core promise (issue #25 P3a phase 4): a running
reservation's topology edits go to the reservation's FORK, so committing appends
a fork version and the parent topology's own version history stays unchanged,
and an ended reservation's fork view renders read-only as the as-built record.

Scenarios:
  a. Open a running reservation's editor and commit, asserting the released/built
     save toast (the commit path: POST /reservations/{id}/fork/save, surfaced as
     a toast). Canvas edge-drawing is not scripted here: React Flow handle drag is
     not reliably drivable through Selenium, and the existing live-edit suite
     avoids it too. A commit still reconciles and appends a fork version, so the
     released/built toast is exercised end to end.
  b. Snapshot the parent topology's version list before and after the commit and
     assert it is byte-for-byte unchanged (the master stays clean).
  c. Complete the reservation and assert the fork view renders read-only
     (as-built), with the commit affordance gone.

Issue #622 (ADR 0006 addendum, fork version preview/diff/restore) adds two more
scenarios at the bottom, in Playwright (the `pw_page` fixture from conftest.py;
NOT the pytest-playwright plugin fixtures, which a local conftest fixture of the
same name shadows for everything under tests/e2e/ -- see the pw_browser
docstring). Same edge-drawing constraint as above, so both scenarios stay within
the single-device (no-edge) fork the setup helper builds: they exercise the
preview lock and the restore/Save round trip through API read-back rather than
proving an actual wire changes, which is honest given the setup has no wire to
begin with.
  d. Preview a version: the read-only history banner appears and the commit
     (Save) affordance disappears while it is up; exiting brings it back.
  e. Restore an older version, then Save: read back through GET
     /reservations/{id}/fork that restore alone appended no new fork_versions
     row (only draft_restored_from_id, cleared again once Save runs) and that
     Save's own new version carries restored_from_id; read back through the
     wiring-status endpoint that the reconcile ran cleanly.

They need a running HERD stack plus the Selenium container (Playwright scenarios
need only the host-side stack, no Selenium); collection alone does not exercise
the browser.
"""

import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import HOST_BASE_URL, api_request, pw_api, pw_login

WAIT = 15
# A freshly created reservation whose start_time is now transitions
# PENDING to PENDING_PROVISION to ACTIVE through the lifecycle. The fork is
# editable (and lazy-creates on first read) only once ACTIVE, so we poll for it.
ACTIVE_POLL_SECONDS = 30


def _create_reserved_topology(browser):
    """Create an empty topology plus an ACTIVE reservation that references it.

    Returns a dict with the reservation and topology, or None if setup cannot be
    satisfied (no devices, create failures, or the reservation never activates).
    The caller skips on None.
    """
    topo_name = f"e2e-fork-{uuid.uuid4().hex[:8]}"
    topo_resp = api_request(
        browser, "POST", "/cabling/topologies", json={"name": topo_name}, allow_errors=True
    )
    if topo_resp.status_code not in (200, 201):
        return None
    topology = topo_resp.json()

    devices_resp = api_request(
        browser, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True
    )
    if devices_resp.status_code != 200:
        api_request(browser, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None
    payload = devices_resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        api_request(browser, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None

    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [available[0]["id"]],
        "purpose": "e2e fork live-edit",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=30)).isoformat(),
        "topology_id": topology["id"],
    }
    # Put the reserved device on the topology canvas (a thin deviceNode ref the
    # editor hydrates by id) so the commit-time device-set PATCH carries the same
    # single device instead of an empty list the backend would reject.
    canvas = {
        "nodes": [
            {
                "id": "n0",
                "type": "deviceNode",
                "position": {"x": 100, "y": 100},
                "data": {"device": {"id": available[0]["id"]}},
            }
        ],
        "edges": [],
    }
    canvas_resp = api_request(
        browser,
        "PUT",
        f"/cabling/topologies/{topology['id']}",
        json={"canvas_data": canvas},
        allow_errors=True,
    )
    if canvas_resp.status_code != 200:
        api_request(browser, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None

    res_resp = api_request(browser, "POST", "/reservations/", json=body, allow_errors=True)
    if res_resp.status_code != 201:
        api_request(browser, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None
    reservation = res_resp.json()

    # Poll until the reservation activates (the fork is editable only when ACTIVE).
    deadline = time.time() + ACTIVE_POLL_SECONDS
    status = reservation.get("status")
    while status != "ACTIVE" and time.time() < deadline:
        time.sleep(1)
        poll = api_request(
            browser, "GET", f"/reservations/{reservation['id']}", allow_errors=True
        )
        if poll.status_code == 200:
            status = poll.json().get("status")
            reservation = poll.json()
        if status in ("FAILED", "CANCELLED", "COMPLETED"):
            break
    if status != "ACTIVE":
        api_request(
            browser, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True
        )
        api_request(browser, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None

    return {"reservation": reservation, "topology": topology}


def _cleanup_reserved_topology(browser, setup):
    if not setup:
        return
    api_request(
        browser, "DELETE", f"/reservations/{setup['reservation']['id']}", allow_errors=True
    )
    api_request(
        browser, "DELETE", f"/cabling/topologies/{setup['topology']['id']}", allow_errors=True
    )


@pytest.fixture(scope="module")
def fork_reservation(admin_browser, base_url):
    """An ACTIVE reservation owning a topology, for the commit and history tests."""
    admin_browser.get(f"{base_url}/topology")
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    setup = _create_reserved_topology(admin_browser)
    if not setup:
        pytest.skip("could not provision an ACTIVE reserved topology for the fork e2e")
    yield setup
    _cleanup_reserved_topology(admin_browser, setup)


def _open_live_editor(driver, base_url, setup):
    reservation = setup["reservation"]
    topology = setup["topology"]
    driver.get(f"{base_url}/topology/{topology['id']}?reservationId={reservation['id']}")
    wait = WebDriverWait(driver, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow")))
    return wait


def test_commit_shows_released_built_save_toast(admin_browser, base_url, fork_reservation):
    """Committing in live-edit mode saves the fork and surfaces a released/built toast."""
    wait = _open_live_editor(admin_browser, base_url, fork_reservation)

    commit = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Commit to reservation']"))
    )
    admin_browser.execute_script("arguments[0].scrollIntoView(true);", commit)
    time.sleep(0.2)
    try:
        commit.click()
    except Exception:
        admin_browser.execute_script("arguments[0].click();", commit)

    # The ForkSaveResultToast renders "Fork saved as v{n}" and the released/built
    # counts. Anchor on the toast's <p> elements: a bare //*[contains(., ...)] also
    # matches every ancestor (html first in document order), and the visibility
    # check then runs against <html>, never the toast.
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//p[contains(normalize-space(.), 'Fork saved as v')]")
        )
    )
    assert admin_browser.find_elements(
        By.XPATH, "//p[contains(., 'Released') and contains(., 'built')]"
    )


def test_parent_topology_history_unchanged_after_commit(
    admin_browser, base_url, fork_reservation
):
    """A fork commit must not append a version to the PARENT topology's history."""
    topology_id = fork_reservation["topology"]["id"]

    def parent_versions():
        resp = api_request(
            admin_browser,
            "GET",
            f"/cabling/topologies/{topology_id}/versions?limit=200",
            allow_errors=True,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body.get("items", body) if isinstance(body, dict) else body
        # Compare a stable projection: the ordered (version_number, id) pairs.
        return [(v["version_number"], v["id"]) for v in items]

    before = parent_versions()

    wait = _open_live_editor(admin_browser, base_url, fork_reservation)
    commit = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Commit to reservation']"))
    )
    admin_browser.execute_script("arguments[0].scrollIntoView(true);", commit)
    time.sleep(0.2)
    try:
        commit.click()
    except Exception:
        admin_browser.execute_script("arguments[0].click();", commit)
    # Anchored on the toast <p>: see the locator note in the toast test above.
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//p[contains(normalize-space(.), 'Fork saved as v')]")
        )
    )

    after = parent_versions()
    assert after == before, (
        "parent topology history changed after a fork commit; the master must stay clean"
    )


def test_ended_reservation_fork_view_is_read_only(admin_browser, base_url):
    """Completing a reservation freezes its fork; the fork view renders read-only."""
    admin_browser.get(f"{base_url}/topology")
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    # A dedicated reservation so completing it does not disturb the module fixture.
    setup = _create_reserved_topology(admin_browser)
    if not setup:
        pytest.skip("could not provision an ACTIVE reserved topology for the read-only e2e")

    try:
        reservation = setup["reservation"]
        topology = setup["topology"]

        # Materialize the fork while ACTIVE (first read lazy-creates it), so an
        # as-built record exists to freeze on completion.
        api_request(
            admin_browser, "GET", f"/reservations/{reservation['id']}/fork", allow_errors=True
        )
        # Complete the reservation (release -> COMPLETED), which archives the fork.
        release = api_request(
            admin_browser, "PUT", f"/reservations/{reservation['id']}/release", allow_errors=True
        )
        assert release.status_code in (200, 204), release.text

        # Open the fork view; it must render read-only (as-built), with no commit.
        admin_browser.get(
            f"{base_url}/topology/{topology['id']}?reservationId={reservation['id']}"
        )
        wait = WebDriverWait(admin_browser, WAIT)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow")))
        wait.until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(translate(., 'abcdefghijklmnopqrstuvwxyz',"
                    " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'AS-BUILT RECORD (READ-ONLY)')]",
                )
            )
        )
        assert (
            admin_browser.find_elements(
                By.XPATH, "//button[normalize-space()='Commit to reservation']"
            )
            == []
        )
    finally:
        _cleanup_reserved_topology(admin_browser, setup)


# --- Playwright: fork version preview and restore (issue #622) ------------


def _pw_create_reserved_topology(page):
    """Playwright counterpart of _create_reserved_topology, using pw_api.

    Returns the same shape (or None to skip), for the same reasons: no
    devices, create failures, or the reservation never activates.
    """
    topo_name = f"e2e-fork-pw-{uuid.uuid4().hex[:8]}"
    topo_resp = pw_api(
        page, "POST", "/cabling/topologies", json={"name": topo_name}, allow_errors=True
    )
    if topo_resp.status_code not in (200, 201):
        return None
    topology = topo_resp.json()

    devices_resp = pw_api(
        page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True
    )
    if devices_resp.status_code != 200:
        pw_api(page, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None
    payload = devices_resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        pw_api(page, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None

    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [available[0]["id"]],
        "purpose": "e2e fork restore",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=30)).isoformat(),
        "topology_id": topology["id"],
    }
    canvas = {
        "nodes": [
            {
                "id": "n0",
                "type": "deviceNode",
                "position": {"x": 100, "y": 100},
                "data": {"device": {"id": available[0]["id"]}},
            }
        ],
        "edges": [],
    }
    canvas_resp = pw_api(
        page,
        "PUT",
        f"/cabling/topologies/{topology['id']}",
        json={"canvas_data": canvas},
        allow_errors=True,
    )
    if canvas_resp.status_code != 200:
        pw_api(page, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None

    res_resp = pw_api(page, "POST", "/reservations/", json=body, allow_errors=True)
    if res_resp.status_code != 201:
        pw_api(page, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None
    reservation = res_resp.json()

    deadline = time.time() + ACTIVE_POLL_SECONDS
    status = reservation.get("status")
    while status != "ACTIVE" and time.time() < deadline:
        time.sleep(1)
        poll = pw_api(page, "GET", f"/reservations/{reservation['id']}", allow_errors=True)
        if poll.status_code == 200:
            status = poll.json().get("status")
            reservation = poll.json()
        if status in ("FAILED", "CANCELLED", "COMPLETED"):
            break
    if status != "ACTIVE":
        pw_api(page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True)
        pw_api(page, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
        return None

    return {"reservation": reservation, "topology": topology}


def _pw_cleanup_reserved_topology(page, setup):
    if not setup:
        return
    pw_api(page, "DELETE", f"/reservations/{setup['reservation']['id']}", allow_errors=True)
    pw_api(page, "DELETE", f"/cabling/topologies/{setup['topology']['id']}", allow_errors=True)


PREVIEW_BANNER_RE = re.compile(r"Previewing version \d+")
FORK_SAVED_TOAST_RE = re.compile(r"Fork saved as v\d+")


def test_preview_shows_history_banner_and_disables_save_pw(pw_page):
    """Previewing a fork version locks Save/commit and shows the banner (issue #622).

    Committing before preview creates fork version 2 (identical to version 1,
    since no edges were ever drawn -- see the module docstring), giving the
    history panel a second, genuinely-previewable row.
    """
    pw_login(pw_page)
    setup = _pw_create_reserved_topology(pw_page)
    if not setup:
        pytest.skip("could not provision an ACTIVE reserved topology for the preview e2e")
    reservation = setup["reservation"]
    topology = setup["topology"]

    try:
        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology['id']}?reservationId={reservation['id']}")
        expect(pw_page.locator(".react-flow")).to_be_visible(timeout=15_000)
        expect(pw_page.get_by_text("EDITING LIVE RESERVATION")).to_be_visible()

        pw_page.get_by_role("button", name="History").click()
        pw_page.get_by_role("button", name="Preview", exact=True).first.click()

        expect(pw_page.get_by_text(PREVIEW_BANNER_RE)).to_be_visible(timeout=15_000)
        expect(pw_page.get_by_text("EDITING LIVE RESERVATION")).not_to_be_visible()
        expect(pw_page.get_by_role("button", name="Commit to reservation")).not_to_be_visible()

        pw_page.get_by_role("button", name=re.compile(r"Exit preview")).click()
        expect(pw_page.get_by_text("EDITING LIVE RESERVATION")).to_be_visible()
    finally:
        _pw_cleanup_reserved_topology(pw_page, setup)


def test_restore_then_save_via_fork_readback_pw(pw_page):
    """Restore an older version, Save, and prove the round trip via API read-back.

    Restore itself must append NO fork_versions row (only set
    draft_restored_from_id); Save's own new version must carry
    restored_from_id pointing at the restored version, and
    draft_restored_from_id must be null again afterward. The wiring-status
    read-back confirms Save's reconcile ran (empty connections either side,
    since this fork never had an edge -- see the module docstring).
    """
    pw_login(pw_page)
    setup = _pw_create_reserved_topology(pw_page)
    if not setup:
        pytest.skip("could not provision an ACTIVE reserved topology for the restore e2e")
    reservation = setup["reservation"]
    topology = setup["topology"]

    def fork_now():
        return pw_api(pw_page, "GET", f"/reservations/{reservation['id']}/fork").json()

    try:
        fork_before = fork_now()
        assert len(fork_before["versions"]) == 1, "fork should start with exactly version 1"
        v1_id = fork_before["versions"][0]["id"]
        assert fork_before["draft_restored_from_id"] is None

        pw_page.goto(f"{HOST_BASE_URL}/topology/{topology['id']}?reservationId={reservation['id']}")
        expect(pw_page.locator(".react-flow")).to_be_visible(timeout=15_000)

        # Commit once with no edits to create version 2 (identical canvas).
        pw_page.get_by_role("button", name="Commit to reservation").click()
        expect(pw_page.get_by_text(FORK_SAVED_TOAST_RE)).to_be_visible(timeout=15_000)
        assert len(fork_now()["versions"]) == 2

        # Restore version 1 through the history panel. The panel lists
        # versions newest-first (cabling orders ForkVersion.version_number
        # desc), so a bare `.first` on the Restore buttons would hit v2's row
        # instead: find the "v1" label span, then walk up to its enclosing
        # row (the span's grandparent: ForkHistoryPanel.tsx nests the
        # version label in `<div className="flex items-center gap-2">`,
        # itself a child of the per-version row div), and restore from
        # inside that specific row.
        pw_page.get_by_role("button", name="History").click()
        v1_label = pw_page.get_by_text("v1", exact=True)
        v1_row = v1_label.locator("xpath=../..")
        v1_row.get_by_role("button", name="Restore", exact=True).click()
        dialog = pw_page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Restore", exact=True).click()

        # Restore appends no version; only draft_restored_from_id is set.
        deadline = time.time() + 15
        fork_after_restore = fork_now()
        while fork_after_restore.get("draft_restored_from_id") != v1_id and time.time() < deadline:
            time.sleep(0.5)
            fork_after_restore = fork_now()
        assert fork_after_restore["draft_restored_from_id"] == v1_id
        assert len(fork_after_restore["versions"]) == 2, "restore alone must not append a version"

        # Save the restored draft: a new version lands, carrying restored_from_id.
        pw_page.get_by_role("button", name="Commit to reservation").click()
        expect(pw_page.get_by_text(FORK_SAVED_TOAST_RE)).to_be_visible(timeout=15_000)

        fork_after_save = fork_now()
        assert len(fork_after_save["versions"]) == 3
        newest = max(fork_after_save["versions"], key=lambda v: v["version_number"])
        assert newest["restored_from_id"] == v1_id
        assert fork_after_save["draft_restored_from_id"] is None

        # Wiring read-back: the reconcile ran; this fork never had an edge, so
        # both sides are empty, but the endpoint answering at all (not 500/503)
        # is itself proof Save's reconcile completed against the restored set.
        wiring = pw_api(pw_page, "GET", f"/reservations/{reservation['id']}/wiring-status").json()
        assert wiring["connections"] == []
    finally:
        _pw_cleanup_reserved_topology(pw_page, setup)
