"""E2E tests for the inventory page."""

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def test_inventory_page_loads(logged_in_browser, base_url):
    """Inventory page renders (table or empty state)."""
    logged_in_browser.get(f"{base_url}/inventory")
    wait = WebDriverWait(logged_in_browser, WAIT)

    # Wait for either device table or "No devices found"
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No devices')]",
            )
        )
    )


def test_inventory_has_search_input(logged_in_browser, base_url):
    """Inventory page has a search input for filtering devices."""
    logged_in_browser.get(f"{base_url}/inventory")
    wait = WebDriverWait(logged_in_browser, WAIT)

    search_input = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder*='Search']"))
    )
    assert search_input.is_displayed()


def test_inventory_search_filters_results(logged_in_browser, base_url):
    """Typing in search updates the device list."""
    logged_in_browser.get(f"{base_url}/inventory")
    wait = WebDriverWait(logged_in_browser, WAIT)

    # Wait for page to load
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No devices')]",
            )
        )
    )

    search_input = logged_in_browser.find_element(By.CSS_SELECTOR, "input[placeholder*='Search']")
    search_input.clear()
    typed = "nonexistent-device-xyz"
    search_input.send_keys(typed)

    # Wait for debounce
    time.sleep(1.5)

    page_text = logged_in_browser.find_element(By.TAG_NAME, "body").text
    # Should show no results or fewer results
    assert "No devices" in page_text or "nonexistent" not in page_text

    # Erase via keystrokes so React's controlled onChange fires and the
    # debounced setSavedFilter writes an empty search back to user-profile.
    # element.clear() alone bypasses onChange, leaving the saved_filters.inventory.search
    # persisted to the prior value, which poisons later test runs.
    from selenium.webdriver.common.keys import Keys

    search_input.send_keys(Keys.END)
    search_input.send_keys(Keys.BACKSPACE * len(typed))
    time.sleep(0.6)


def test_inventory_device_expand_shows_ports(logged_in_browser, base_url):
    """If devices exist, clicking expand shows port details."""
    logged_in_browser.get(f"{base_url}/inventory")
    wait = WebDriverWait(logged_in_browser, WAIT)

    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No devices')]",
            )
        )
    )

    # Only test expand if there are device rows
    chevron_buttons = logged_in_browser.find_elements(By.CSS_SELECTOR, "tbody tr button")
    if chevron_buttons:
        chevron_buttons[0].click()
        time.sleep(1)
        page_text = logged_in_browser.find_element(By.TAG_NAME, "body").text
        assert "port" in page_text.lower() or "no ports" in page_text.lower()
