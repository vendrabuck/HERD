"""E2E tests for cancelling and releasing a reservation from the table.

GAPS coverage: the reservation cancel and release lifecycle through the UI.
Existing tests create and cancel reservations via the API only; none clicked
the row's Cancel or Release action, exercised the ConfirmDialog, or pinned
the rendered CANCELLED / COMPLETED badge. The ConfirmDialog is a native
<dialog> element opened with
showModal, so its buttons are regular DOM elements (clickable directly);
this is not a window.confirm browser alert, which would need Selenium's
Alert API instead.

The row's Cancel and Release actions render only while the reservation is
ACTIVE, and a freshly created exclusive reservation starts in
PENDING_PROVISION until the execution pipeline provisions it. Each test
therefore polls the API until the transient reservation reaches ACTIVE and
skips (rather than fails) if the stack never activates it.

Every ReservationRow mounts its own (closed) ConfirmDialog sharing the same
element ids, so assertions target the single `dialog[open]` element rather
than By.ID, which would resolve to the first row's closed dialog.
"""

import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15
ACTIVATION_TIMEOUT = 60


def _wait_for_active(driver, reservation_id):
    """Poll the API until the reservation is ACTIVE; skip on timeout or terminal."""
    deadline = time.time() + ACTIVATION_TIMEOUT
    status = None
    while time.time() < deadline:
        resp = api_request(driver, "GET", f"/reservations/{reservation_id}", allow_errors=True)
        if resp.status_code == 200:
            status = resp.json()["status"]
            if status == "ACTIVE":
                return
            if status in ("CANCELLED", "FAILED", "COMPLETED"):
                pytest.skip(f"reservation hit terminal status {status} before the test ran")
        time.sleep(2)
    pytest.skip(f"reservation never became ACTIVE (last status: {status})")


def _open_reservations(driver, base_url):
    driver.get(f"{base_url}/reservations")
    WebDriverWait(driver, WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))


def _click_cancel_action(driver, reservation):
    """Click the row-level Cancel button for this reservation (by aria-label)."""
    short_id = reservation["id"][:8]
    btn = WebDriverWait(driver, WAIT).until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, f"button[aria-label='Cancel reservation {short_id}']")
        )
    )
    btn.click()


def _click_release_action(driver, reservation):
    """Click the row-level Release button for this reservation (by aria-label)."""
    short_id = reservation["id"][:8]
    btn = WebDriverWait(driver, WAIT).until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, f"button[aria-label='Release reservation {short_id}']")
        )
    )
    btn.click()


def _wait_open_dialog(driver):
    """Return the open ConfirmDialog element once visible."""
    return WebDriverWait(driver, WAIT).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "dialog[open]"))
    )


def _wait_dialog_closed(driver):
    WebDriverWait(driver, WAIT).until(
        lambda d: not d.find_elements(By.CSS_SELECTOR, "dialog[open]")
    )


def test_cancel_dialog_keep_leaves_reservation_active(
    admin_browser, base_url, transient_reservation
):
    """Opening the cancel dialog and choosing Keep changes nothing."""
    _wait_for_active(admin_browser, transient_reservation["id"])
    _open_reservations(admin_browser, base_url)
    _click_cancel_action(admin_browser, transient_reservation)

    dialog = _wait_open_dialog(admin_browser)
    title = dialog.find_element(By.CSS_SELECTOR, "h2")
    assert title.text == "Cancel Reservation"
    desc = dialog.find_element(By.TAG_NAME, "p")
    assert "cannot be undone" in desc.text

    dialog.find_element(By.XPATH, ".//button[normalize-space()='Keep reservation']").click()
    _wait_dialog_closed(admin_browser)

    resp = api_request(admin_browser, "GET", f"/reservations/{transient_reservation['id']}")
    assert resp.json()["status"] == "ACTIVE"


def test_cancel_reservation_via_ui(admin_browser, base_url, transient_reservation):
    """Confirming the cancel dialog cancels the reservation and updates the row."""
    _wait_for_active(admin_browser, transient_reservation["id"])
    _open_reservations(admin_browser, base_url)
    _click_cancel_action(admin_browser, transient_reservation)

    dialog = _wait_open_dialog(admin_browser)
    dialog.find_element(By.XPATH, ".//button[normalize-space()='Cancel reservation']").click()
    _wait_dialog_closed(admin_browser)

    # The mutation invalidates the reservations query; the row's badge flips
    # to the verbatim backend enum text.
    short_id = transient_reservation["id"][:8]
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                f"//tbody/tr[td[normalize-space()='{short_id}']]//*[normalize-space()='CANCELLED']",
            )
        )
    )

    resp = api_request(admin_browser, "GET", f"/reservations/{transient_reservation['id']}")
    assert resp.json()["status"] == "CANCELLED"


def test_release_reservation_via_ui(admin_browser, base_url, transient_reservation):
    """Clicking Release completes the reservation and flips the row badge.

    Release fires immediately with no ConfirmDialog (unlike Cancel), so the
    test clicks the row action and then waits for the backend's COMPLETED enum
    to surface both in the badge and via the API. The Release and Cancel
    actions render only while the reservation is ACTIVE.
    """
    _wait_for_active(admin_browser, transient_reservation["id"])
    _open_reservations(admin_browser, base_url)
    _click_release_action(admin_browser, transient_reservation)

    # The mutation invalidates the reservations query; the row's badge flips
    # to the verbatim backend enum text and the row's actions disappear.
    short_id = transient_reservation["id"][:8]
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                f"//tbody/tr[td[normalize-space()='{short_id}']]//*[normalize-space()='COMPLETED']",
            )
        )
    )

    resp = api_request(admin_browser, "GET", f"/reservations/{transient_reservation['id']}")
    assert resp.json()["status"] == "COMPLETED"
