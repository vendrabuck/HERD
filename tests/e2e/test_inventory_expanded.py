"""E2E tests for the inventory expanded device row."""

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_inventory(driver, base_url):
    driver.get(f"{base_url}/inventory")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No devices')]",
            )
        )
    )


def _expand_first_device(driver):
    """Click the first device row's chevron. Returns True if a row was found."""
    chevrons = driver.find_elements(
        By.CSS_SELECTOR, "tbody tr button[aria-label*='Expand']"
    )
    if not chevrons:
        return False
    chevrons[0].click()
    return True


def test_inventory_expanded_shows_device_info_panel(logged_in_browser, base_url):
    """Expanding a device row renders the DeviceInfoPanel with Dates and Audit."""
    _open_inventory(logged_in_browser, base_url)
    if not _expand_first_device(logged_in_browser):
        import pytest

        pytest.skip("no devices to expand")

    time.sleep(0.5)
    body = logged_in_browser.find_element(By.TAG_NAME, "body").text
    assert "Created:" in body or "Dates" in body.upper() or "DATES" in body
    assert "Created by:" in body or "AUDIT" in body.upper() or "Audit" in body


def test_inventory_expanded_shows_ports_section(logged_in_browser, base_url):
    """Expanding a device shows the ports sub-table header 'Connected To'."""
    _open_inventory(logged_in_browser, base_url)
    if not _expand_first_device(logged_in_browser):
        import pytest

        pytest.skip("no devices to expand")

    time.sleep(0.5)
    body = logged_in_browser.find_element(By.TAG_NAME, "body").text
    # Either the ports table renders (with a 'connected to' header, uppercased
    # by CSS text-transform) or the 'No ports configured' empty state shows.
    body_lower = body.lower()
    assert "connected to" in body_lower or "no ports" in body_lower


def test_inventory_expanded_shows_device_groups_section(logged_in_browser, base_url):
    """Expanding a device shows a Device Groups section."""
    _open_inventory(logged_in_browser, base_url)
    if not _expand_first_device(logged_in_browser):
        import pytest

        pytest.skip("no devices to expand")

    time.sleep(1.0)  # allow device groups lookup to resolve
    body = logged_in_browser.find_element(By.TAG_NAME, "body").text
    assert "Device Groups" in body or "DEVICE GROUPS" in body.upper()
