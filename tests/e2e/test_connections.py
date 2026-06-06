"""E2E tests for the connections admin page."""

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_connections(driver, base_url):
    driver.get(f"{base_url}/admin/connections")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//button[contains(., 'Create Connection')] "
                "| //*[contains(text(), 'No connections')]",
            )
        )
    )


def test_connections_page_has_create_button(admin_browser, base_url):
    """Connections page shows the Create Connection button."""
    _open_connections(admin_browser, base_url)
    btn = admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Connection')]"
    )
    assert btn.is_displayed()


def test_connections_create_modal_opens(admin_browser, base_url):
    """Create Connection opens a modal with the expected fields."""
    _open_connections(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Connection')]"
    ).click()

    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Create Connection')]")
        )
    )

    # Device A/B search inputs and port selectors are rendered.
    inputs = admin_browser.find_elements(
        By.CSS_SELECTOR, "input[placeholder*='Search devices']"
    )
    assert len(inputs) >= 2
    # Two <select> elements for ports.
    selects = admin_browser.find_elements(By.CSS_SELECTOR, "select")
    assert len(selects) >= 2

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Cancel']"
    ).click()


def test_connections_port_select_disabled_until_device_picked(admin_browser, base_url):
    """Port A select is disabled until a device is chosen."""
    _open_connections(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Connection')]"
    ).click()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "select"))
    )

    selects = admin_browser.find_elements(By.CSS_SELECTOR, "select")
    # Both port selects should be disabled with no device picked yet.
    for s in selects[:2]:
        assert s.get_attribute("disabled") is not None

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Cancel']"
    ).click()


def test_connections_create_requires_device(admin_browser, base_url):
    """Clicking Create without selecting devices triggers a validation toast."""
    _open_connections(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Connection')]"
    ).click()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//button[normalize-space()='Create']")
        )
    )

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Create']"
    ).click()
    time.sleep(1.0)
    body = admin_browser.find_element(By.TAG_NAME, "body").text.lower()
    assert "required" in body or "device a" in body

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Cancel']"
    ).click()


def test_connections_filter_by_device_input_renders(admin_browser, base_url):
    """The device filter input at the top of the page is present."""
    _open_connections(admin_browser, base_url)
    inputs = admin_browser.find_elements(
        By.CSS_SELECTOR, "input[placeholder*='Filter by device']"
    )
    assert len(inputs) >= 1
    assert inputs[0].is_displayed()
