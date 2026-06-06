"""E2E tests for device groups admin pages."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_device_groups(driver, base_url):
    driver.get(f"{base_url}/admin/device-groups")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "table"))
    )


def test_device_groups_list_shows_create_button(admin_browser, base_url):
    """Device groups list has the Create Device Group button."""
    _open_device_groups(admin_browser, base_url)
    btn = admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Device Group')]"
    )
    assert btn.is_displayed()


def test_device_groups_list_shows_no_pool(admin_browser, base_url):
    """The seeded 'No Pool' default group shows up in the list."""
    _open_device_groups(admin_browser, base_url)
    body = admin_browser.find_element(By.TAG_NAME, "body").text
    assert "No Pool" in body


def test_device_groups_new_page_renders_form(admin_browser, base_url):
    """Clicking Create opens the New Device Group form with a name field."""
    _open_device_groups(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Device Group')]"
    ).click()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.url_contains("/admin/device-groups/new"))
    wait.until(EC.presence_of_element_located((By.ID, "dg-name")))
    save_btn = admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Create']"
    )
    assert save_btn.is_displayed()


def test_device_groups_no_pool_detail_renders(admin_browser, base_url):
    """Opening the No Pool group loads the detail page with devices/permissions sections."""
    _open_device_groups(admin_browser, base_url)
    row = admin_browser.find_element(
        By.XPATH, "//tr[td[normalize-space()='No Pool']]"
    )
    row.click()

    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "dg-name")))
    # Devices and Permissions sections show up only once the detail has loaded.
    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//button[contains(., 'Add or Remove Devices')]")
        )
    )


def test_device_group_add_devices_modal_opens(admin_browser, base_url):
    """Clicking 'Add or Remove Devices' opens a modal with the TransferList."""
    _open_device_groups(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//tr[td[normalize-space()='No Pool']]"
    ).click()

    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(., 'Add or Remove Devices')]")
        )
    ).click()

    # TransferList renders two side-by-side list panes.
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Available')]")
        )
    )
    assigned_hdr = admin_browser.find_elements(
        By.XPATH, "//*[contains(text(), 'Assigned') or contains(text(), 'Selected')]"
    )
    assert len(assigned_hdr) >= 1
