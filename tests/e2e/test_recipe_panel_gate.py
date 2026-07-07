"""E2E tests for the AI recipe-drafting panel gate on the drivers page (issue #28).

The 'Draft with AI' button renders only when /api/ai/status reports both
enabled and recipe_authoring true. Checked against the live status so the
test gives consistent results in either stack configuration (the shipped
default is recipe_authoring false, so the standard e2e stack exercises the
hidden branch).
"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15


def _recipe_authoring_on(driver) -> bool:
    resp = api_request(driver, "GET", "/ai/status", allow_errors=True)
    assert resp.status_code == 200, f"expected /ai/status 200, got {resp.status_code}"
    body = resp.json()
    return bool(body.get("enabled")) and bool(body.get("recipe_authoring"))


def _open_drivers_page(browser, base_url):
    browser.get(f"{base_url}/admin/drivers")
    WebDriverWait(browser, WAIT).until(
        EC.presence_of_element_located((By.XPATH, "//h2[normalize-space()='Drivers']"))
    )


def _draft_buttons(browser):
    return browser.find_elements(By.XPATH, "//button[normalize-space()='Draft with AI']")


def test_status_reports_recipe_authoring_flag(admin_browser):
    """The status payload carries the recipe_authoring boolean the UI keys off."""
    resp = api_request(admin_browser, "GET", "/ai/status", allow_errors=True)
    assert resp.status_code == 200
    assert isinstance(resp.json().get("recipe_authoring"), bool)


def test_draft_with_ai_button_matches_status(admin_browser, base_url):
    """The button renders exactly when the live status enables the feature."""
    _open_drivers_page(admin_browser, base_url)
    buttons = _draft_buttons(admin_browser)
    if _recipe_authoring_on(admin_browser):
        assert len(buttons) == 1, "flag on but Draft with AI button missing"
    else:
        assert buttons == [], "flag off but Draft with AI button rendered"


def test_upload_dialog_offers_hypervisor_connection_type(admin_browser, base_url):
    """Recipes are Hypervisor packages; the manual upload path must offer the type."""
    _open_drivers_page(admin_browser, base_url)
    admin_browser.find_element(By.XPATH, "//button[normalize-space()='Upload Driver']").click()
    select = WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located((By.ID, "driver-connection-type"))
    )
    options = [o.text for o in select.find_elements(By.TAG_NAME, "option")]
    assert "Hypervisor" in options
