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
    """Each of the five tabs can be activated.

    Issue #1148: the old check (the page body is displayed) held for a no-op tab.
    Now the fixture's own row is opened (by its short id cell), and after each
    click the clicked tab is the ONE active tab (the bar marks it with
    bg-gray-900) and the Details panel's Owner row is shown only on Details.
    """
    _open_reservations(admin_browser, base_url)
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    short_id = transient_reservation["id"][:8]
    row_cell = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, f"//tbody/tr/td[normalize-space()='{short_id}']")
        )
    )
    row_cell.click()
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//dialog[@open]//button[normalize-space()='Inventory']")
        )
    )

    labels = ("Details", "Inventory", "Routes", "Wiring", "Schedule")
    owner_row = (By.XPATH, "//dialog[@open]//span[normalize-space()='Owner']")
    for label in ("Inventory", "Routes", "Wiring", "Schedule", "Details"):
        admin_browser.find_element(
            By.XPATH, f"//dialog[@open]//button[normalize-space()='{label}']"
        ).click()
        wait.until(
            lambda d, label=label: "bg-gray-900"
            in d.find_element(
                By.XPATH, f"//dialog[@open]//button[normalize-space()='{label}']"
            ).get_attribute("class")
        )
        active = [
            other
            for other in labels
            if "bg-gray-900"
            in admin_browser.find_element(
                By.XPATH, f"//dialog[@open]//button[normalize-space()='{other}']"
            ).get_attribute("class")
        ]
        assert active == [label], f"after clicking {label}, active tabs were {active}"
        shows_owner = bool(admin_browser.find_elements(*owner_row))
        assert shows_owner == (label == "Details"), (
            f"the Details panel's Owner row {'is' if shows_owner else 'is not'} shown "
            f"on the {label} tab"
        )


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
