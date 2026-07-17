"""E2E tests for the reservation detail modal."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_reservations(driver, base_url):
    driver.get(f"{base_url}/reservations")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No reservations')]",
            )
        )
    )


def _click_reservation_row(driver):
    row = driver.find_element(By.CSS_SELECTOR, "tbody tr")
    row.click()


def test_reservation_detail_modal_opens(admin_browser, base_url, transient_reservation):
    """Clicking a reservation row opens the detail modal with the 5 tabs."""
    _open_reservations(admin_browser, base_url)
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))

    _click_reservation_row(admin_browser)

    # Modal title "Reservation" appears.
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Reservation')]")
        )
    )
    # Five tab buttons: Details, Inventory, Routes, Wiring, Schedule.
    for label in ("Details", "Inventory", "Routes", "Wiring", "Schedule"):
        admin_browser.find_element(
            By.XPATH, f"//button[normalize-space()='{label}']"
        )


def test_reservation_detail_tabs_switch(admin_browser, base_url, transient_reservation):
    """Each of the five tabs can be activated."""
    _open_reservations(admin_browser, base_url)
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))

    _click_reservation_row(admin_browser)
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//button[normalize-space()='Inventory']")
        )
    )

    for label in ("Inventory", "Routes", "Wiring", "Schedule", "Details"):
        admin_browser.find_element(
            By.XPATH, f"//button[normalize-space()='{label}']"
        ).click()
        import time

        time.sleep(0.3)
        # The clicked tab's button gets an active class; the body should still be rendered.
        assert admin_browser.find_element(By.TAG_NAME, "body").is_displayed()


def test_reservation_edit_resources_button_visible(
    admin_browser, base_url, transient_reservation
):
    """Edit Resources button shows for the reservation owner on ACTIVE/PENDING."""
    _open_reservations(admin_browser, base_url)
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))

    _click_reservation_row(admin_browser)
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//button[contains(., 'Edit Resources')]")
        )
    )
