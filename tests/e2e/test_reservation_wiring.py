"""E2E test for the reservation-detail wiring-status panel (ADR 0007, #345 P3b).

Covers the phase-5 promise: a running reservation's detail modal exposes a Wiring
tab that renders the per-connection L1 wiring status proxied from execution
(GET /reservations/{id}/wiring-status).

Depth shipped: the read-only panel-rendering journey. Forcing a live FAILED
cross-connect row through the UI is heavy: it needs an L1-connected topology whose
switch runs the mock_l1 driver with a mock_fail_actions knob, plus a fork save that
rebuilds that connection, and fork saves depend on React Flow handle-drag that the
sibling fork e2e (test_fork_live_edit.py) documents as not reliably drivable through
Selenium. The api_request seeding the fork e2e uses cannot cheaply manufacture a
FAILED row either: l1_connection_assignments rows are written execution-side only
when an L1-connected reservation provisions, and there is no API to insert one. So
this asserts the panel mounts and its query resolves (the empty state for a
reservation with no L1 wiring), which exercises the reservations proxy plus the
panel end to end without depending on seeded L1 wiring. The FAILED-row retry-button
journey is covered by the vitest rendering matrix and the backend integration tests.

Needs a running HERD stack plus the Selenium container; collection alone does not
exercise the browser.
"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_reservations(driver, base_url):
    driver.get(f"{base_url}/reservations")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (By.XPATH, "//table | //*[contains(text(), 'No reservations')]")
        )
    )


def test_wiring_tab_renders_panel(admin_browser, base_url, transient_reservation):
    """Opening the Wiring tab renders the per-connection wiring panel.

    The reservation carries no L1 topology, so the panel resolves to its empty
    state; the assertion is that the tab mounts and its query resolves (neither a
    load spinner left hanging nor the error state), proving the reservations
    wiring-status proxy and the panel work end to end.
    """
    _open_reservations(admin_browser, base_url)
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))

    admin_browser.find_element(By.CSS_SELECTOR, "tbody tr").click()

    # The Wiring tab button appears in the modal's tab bar; click it.
    wiring_tab = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Wiring']"))
    )
    wiring_tab.click()

    # The panel either lists per-connection rows or, for a reservation with no L1
    # wiring, shows the empty-state message. Either proves the panel mounted and its
    # query resolved (not the error state). A topology-less reservation hits the
    # empty state deterministically.
    wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//*[contains(normalize-space(.), 'No per-connection wiring')]"
                " | //*[contains(normalize-space(.), 'Applied fork version')]",
            )
        )
    )
    # The error state must NOT be showing.
    assert (
        admin_browser.find_elements(
            By.XPATH, "//*[contains(normalize-space(.), 'Failed to load wiring status')]"
        )
        == []
    )
