"""E2E tests for editing a running reservation's topology (live-edit mode).

Covers the end-user flow: a Reservations page row opens the
ReservationDetailModal, "Edit topology" navigates to
/topology/{topology_id}?reservationId={reservation.id}, the live-edit editor.

In live-edit mode the editor shows an "Editing reservation" toolbar badge and a
blue LiveEditBar reading "EDITING LIVE RESERVATION" with "Commit to reservation"
and "Cancel" buttons, while the normal "Save" and "Reserve Topology" buttons are
hidden. "Cancel" returns to /reservations.

These tests need a running HERD stack plus the Selenium container; collection
alone does not exercise the browser.
"""

import time
import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15


@pytest.fixture(scope="module")
def reserved_topology(admin_browser, base_url):
    """Provision a reservation that owns a topology, for the admin_browser user.

    The "Edit topology" button only renders when the reservation has a
    topology_id, the browser user is the owner, and the status is ACTIVE or
    PENDING. So we:
      1. create an empty topology via the cabling service (no edges, so the
         reservation service's connectivity validation passes),
      2. pick an available exclusive device,
      3. create a reservation referencing that topology_id, owned by the
         admin_browser user (its JWT signs the call).

    Yields a dict with the created reservation and topology, then best-effort
    deletes both. Skips the test when the inventory or topology setup cannot be
    satisfied (e.g. no available devices).
    """
    from datetime import datetime, timedelta, timezone

    # Navigate so the browser has a live session before we read its JWT.
    admin_browser.get(f"{base_url}/topology")
    WebDriverWait(admin_browser, WAIT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    # 1. Create an empty topology. With no edges it is connectivity-valid, so the
    # reservation service accepts the topology_id reference.
    topo_name = f"e2e-live-edit-{uuid.uuid4().hex[:8]}"
    topo_resp = api_request(
        admin_browser,
        "POST",
        "/cabling/topologies",
        json={"name": topo_name},
        allow_errors=True,
    )
    if topo_resp.status_code not in (200, 201):
        pytest.skip(f"could not create topology: {topo_resp.status_code} {topo_resp.text}")
    topology = topo_resp.json()

    # 2. Pick an available exclusive device to reserve.
    devices_resp = api_request(
        admin_browser,
        "GET",
        "/inventory/devices?limit=100&dut_only=true",
        allow_errors=True,
    )
    if devices_resp.status_code != 200:
        api_request(
            admin_browser,
            "DELETE",
            f"/cabling/topologies/{topology['id']}",
            allow_errors=True,
        )
        pytest.skip(f"cannot list devices: {devices_resp.status_code}")

    payload = devices_resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        api_request(
            admin_browser,
            "DELETE",
            f"/cabling/topologies/{topology['id']}",
            allow_errors=True,
        )
        pytest.skip("no available exclusive devices to reserve")

    # 3. Create the reservation that references the topology. Owned by the
    # browser user since api_request signs with the browser's JWT.
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [available[0]["id"]],
        "purpose": "e2e live-edit reservation",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=30)).isoformat(),
        "topology_id": topology["id"],
    }
    res_resp = api_request(admin_browser, "POST", "/reservations/", json=body, allow_errors=True)
    if res_resp.status_code != 201:
        api_request(
            admin_browser,
            "DELETE",
            f"/cabling/topologies/{topology['id']}",
            allow_errors=True,
        )
        pytest.skip(f"reservation create failed: {res_resp.status_code} {res_resp.text}")
    reservation = res_resp.json()

    yield {"reservation": reservation, "topology": topology}

    api_request(
        admin_browser,
        "DELETE",
        f"/reservations/{reservation['id']}",
        allow_errors=True,
    )
    api_request(
        admin_browser,
        "DELETE",
        f"/cabling/topologies/{topology['id']}",
        allow_errors=True,
    )


def _open_reservations(driver, base_url):
    driver.get(f"{base_url}/reservations")
    driver.refresh()
    WebDriverWait(driver, WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))


def _open_detail_modal_for(driver, base_url, reservation):
    """Open the reservations list and click the row for the given reservation.

    Falls back to the first row when no row carries the reservation id, which
    keeps the test resilient to the row markup not exposing the id; the
    reserved_topology fixture only creates a single fresh reservation, so the
    first row is overwhelmingly the right one after a refresh.
    """
    _open_reservations(driver, base_url)
    wait = WebDriverWait(driver, WAIT)
    rows = driver.find_elements(
        By.XPATH, f"//tbody/tr[contains(@data-reservation-id, '{reservation['id']}')]"
    )
    (rows[0] if rows else driver.find_element(By.CSS_SELECTOR, "tbody tr")).click()
    # Wait for the modal titled "Reservation".
    wait.until(EC.visibility_of_element_located((By.XPATH, "//*[contains(text(), 'Reservation')]")))


def test_edit_topology_button_present_for_reserved_topology(
    admin_browser, base_url, reserved_topology
):
    """The "Edit topology" button shows in the detail modal for a reservation
    that owns a topology and is owned by the browser user."""
    _open_detail_modal_for(admin_browser, base_url, reserved_topology["reservation"])
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(
        EC.visibility_of_element_located((By.XPATH, "//button[normalize-space()='Edit topology']"))
    )
    # Edit Resources sits next to it; confirm both render together.
    assert admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Edit Resources')]"
    ).is_displayed()


def test_edit_topology_lands_on_live_edit_editor(admin_browser, base_url, reserved_topology):
    """Clicking "Edit topology" navigates to the topology editor in live-edit
    mode (URL has /topology/ and reservationId=), and the live-edit badge and
    bar are visible."""
    _open_detail_modal_for(admin_browser, base_url, reserved_topology["reservation"])
    wait = WebDriverWait(admin_browser, WAIT)
    edit_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Edit topology']"))
    )
    edit_btn.click()

    # URL carries both the topology path and the reservationId query param.
    wait.until(EC.url_contains("/topology/"))
    wait.until(EC.url_contains("reservationId="))
    current = admin_browser.current_url
    assert "/topology/" in current
    assert "reservationId=" in current

    # The blue LiveEditBar renders its label. CSS text-transform is not applied
    # to this label (it is literally uppercase in the markup), so match the
    # uppercase form but compare case-insensitively to be safe.
    wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//*[contains(translate(., 'abcdefghijklmnopqrstuvwxyz',"
                " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'EDITING LIVE RESERVATION')]",
            )
        )
    )

    # The toolbar "Editing reservation" badge is present.
    badge = wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//*[contains(translate(., 'abcdefghijklmnopqrstuvwxyz',"
                " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'EDITING RESERVATION')]",
            )
        )
    )
    assert badge.is_displayed()


def test_live_edit_hides_save_and_reserve_shows_commit_and_cancel(
    admin_browser, base_url, reserved_topology
):
    """In live-edit mode "Save" and "Reserve Topology" are absent while
    "Commit to reservation" and "Cancel" are present."""
    reservation = reserved_topology["reservation"]
    topology = reserved_topology["topology"]
    # Navigate straight into live-edit mode via URL; the button flow is covered
    # by the test above, and this keeps this assertion focused on the editor.
    admin_browser.get(f"{base_url}/topology/{topology['id']}?reservationId={reservation['id']}")
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow")))

    # The LiveEditBar action buttons are present.
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//button[normalize-space()='Commit to reservation']")
        )
    )
    assert admin_browser.find_elements(By.XPATH, "//button[normalize-space()='Cancel']")

    # The non-live-edit toolbar buttons are gone. "Reserve Topology" renders with
    # a device-count suffix, so match by prefix; "Save" is an exact label (and
    # must not collide with "Save as Template", so use exact match).
    assert (
        admin_browser.find_elements(
            By.XPATH, "//button[starts-with(normalize-space(), 'Reserve Topology')]"
        )
        == []
    )
    assert admin_browser.find_elements(By.XPATH, "//button[normalize-space()='Save']") == []


def test_live_edit_cancel_returns_to_reservations(admin_browser, base_url, reserved_topology):
    """Clicking "Cancel" in live-edit mode navigates back to /reservations."""
    reservation = reserved_topology["reservation"]
    topology = reserved_topology["topology"]
    admin_browser.get(f"{base_url}/topology/{topology['id']}?reservationId={reservation['id']}")
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow")))

    cancel = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Cancel']"))
    )
    # Native-dialog-style click hardening: scroll into view, then click via JS in
    # case an overlay intercepts the pointer. The Cancel button here is a plain
    # toolbar button, but this keeps the navigation deterministic.
    admin_browser.execute_script("arguments[0].scrollIntoView(true);", cancel)
    time.sleep(0.2)
    try:
        cancel.click()
    except Exception:
        admin_browser.execute_script("arguments[0].click();", cancel)

    wait.until(EC.url_matches(r".*/reservations/?$"))
    assert admin_browser.current_url.rstrip("/").endswith("/reservations")
