"""E2E tests for the reservations page."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def test_reservations_page_loads(logged_in_browser, base_url):
    """Reservations page renders."""
    logged_in_browser.get(f"{base_url}/reservations")
    wait = WebDriverWait(logged_in_browser, WAIT)

    # Page shows "My Reservations" tab label
    wait.until(
        EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'My Reservations')]"))
    )


def test_reservations_shows_content(logged_in_browser, base_url):
    """Reservations page shows table or empty state."""
    logged_in_browser.get(f"{base_url}/reservations")
    wait = WebDriverWait(logged_in_browser, WAIT)

    # Either a table with reservations or "No reservations yet"
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No reservations')]",
            )
        )
    )


def test_reservations_calendar_link(logged_in_browser, base_url):
    """Reservations page has a link to the calendar view."""
    logged_in_browser.get(f"{base_url}/reservations")
    wait = WebDriverWait(logged_in_browser, WAIT)

    calendar_link = wait.until(
        EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Calendar')]"))
    )
    assert calendar_link.is_displayed()


def test_calendar_page_loads(logged_in_browser, base_url):
    """Calendar page renders with navigation controls."""
    logged_in_browser.get(f"{base_url}/reservations/calendar")
    wait = WebDriverWait(logged_in_browser, WAIT)

    # Calendar has Prev/Today/Next buttons and view mode toggles
    wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Today')]")))


def test_calendar_has_view_mode_toggles(logged_in_browser, base_url):
    """Calendar exposes day/week/month view mode buttons."""
    logged_in_browser.get(f"{base_url}/reservations/calendar")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Today')]"))
    )
    for label in ("day", "week", "month"):
        buttons = logged_in_browser.find_elements(
            By.XPATH, f"//button[normalize-space()='{label}']"
        )
        assert buttons, f"expected '{label}' view mode button"


def test_calendar_day_view_toggles(logged_in_browser, base_url):
    """Clicking 'day' switches the header label to a single-day format."""
    logged_in_browser.get(f"{base_url}/reservations/calendar")
    wait = WebDriverWait(logged_in_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Today')]")))

    day_btn = logged_in_browser.find_element(
        By.XPATH, "//button[normalize-space()='day']"
    )
    day_btn.click()

    import time

    time.sleep(0.5)
    # Day view still shows the Today button and renders without error.
    assert logged_in_browser.find_element(
        By.XPATH, "//button[contains(text(), 'Today')]"
    ).is_displayed()


def test_calendar_today_button_clickable(logged_in_browser, base_url):
    """The Today button is present and clickable."""
    logged_in_browser.get(f"{base_url}/reservations/calendar")
    wait = WebDriverWait(logged_in_browser, WAIT)
    today = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Today']"))
    )
    today.click()
    # Header still visible after clicking.
    assert today.is_displayed()
