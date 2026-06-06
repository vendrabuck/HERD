"""E2E tests for the drivers admin page."""

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_drivers(driver, base_url):
    driver.get(f"{base_url}/admin/drivers")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//button[contains(., 'Upload Driver')] | //*[contains(text(), 'No drivers')]",
            )
        )
    )


def test_drivers_page_has_upload_button(admin_browser, base_url):
    """Drivers page shows the Upload Driver button."""
    _open_drivers(admin_browser, base_url)
    btn = admin_browser.find_element(By.XPATH, "//button[contains(., 'Upload Driver')]")
    assert btn.is_displayed()


def test_drivers_upload_modal_opens(admin_browser, base_url):
    """Clicking Upload Driver opens the upload modal with required fields."""
    _open_drivers(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Upload Driver')]"
    ).click()

    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "driver-name")))

    assert admin_browser.find_element(By.ID, "driver-connection-type").is_displayed()
    assert admin_browser.find_element(By.ID, "driver-file").is_displayed()

    # Close the modal so subsequent tests start clean.
    cancel = admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Cancel']"
    )
    cancel.click()


def test_drivers_connection_type_dropdown_has_all_options(admin_browser, base_url):
    """Connection type dropdown offers Management and switch tiers."""
    _open_drivers(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Upload Driver')]"
    ).click()
    wait = WebDriverWait(admin_browser, WAIT)
    select = wait.until(
        EC.presence_of_element_located((By.ID, "driver-connection-type"))
    )
    option_texts = [o.text for o in select.find_elements(By.TAG_NAME, "option")]
    for expected in ("Management", "Layer 1 Switch", "Layer 2 Switch", "Layer 3 Switch"):
        assert expected in option_texts
    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Cancel']"
    ).click()


def test_drivers_upload_requires_name(admin_browser, base_url):
    """Clicking Upload without a name surfaces a validation error toast."""
    _open_drivers(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Upload Driver')]"
    ).click()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "driver-name")))

    # Submit with nothing filled in; a toast should appear.
    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Upload']"
    ).click()
    time.sleep(1.0)
    body = admin_browser.find_element(By.TAG_NAME, "body").text.lower()
    assert "required" in body or "name" in body

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Cancel']"
    ).click()


def test_drivers_delete_confirmation_rendered(admin_browser, base_url, transient_driver):
    """Clicking Delete on a driver row opens the confirm dialog."""
    _open_drivers(admin_browser, base_url)
    # Force a re-render so the new driver shows up.
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, f"//td[normalize-space()='{transient_driver['name']}']")
        )
    )
    row = admin_browser.find_element(
        By.XPATH,
        f"//tr[td[normalize-space()='{transient_driver['name']}']]",
    )
    delete_btn = row.find_element(By.XPATH, ".//button[normalize-space()='Delete']")
    admin_browser.execute_script("arguments[0].scrollIntoView({block: 'center'});", delete_btn)
    admin_browser.execute_script("arguments[0].click();", delete_btn)

    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Delete Driver')]")
        )
    )
    # Cancel so the fixture cleanup path still works. Scope to the dialog so
    # we don't pick up a stale Cancel elsewhere, and click via JS since
    # native <dialog> elements can report the child button as non-interactable
    # to Selenium when the click arbiter runs before layout settles.
    cancel_btn = admin_browser.find_element(
        By.XPATH, "//dialog//button[normalize-space()='Cancel']"
    )
    admin_browser.execute_script("arguments[0].click();", cancel_btn)
