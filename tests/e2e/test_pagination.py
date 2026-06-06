"""E2E tests for the Pagination component on list pages."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _wait_table_or_empty(driver):
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No ') and contains(text(), 'found')]",
            )
        )
    )


def test_users_pagination_hidden_when_under_limit(admin_browser, base_url):
    """With < 50 users, pagination controls are not rendered."""
    admin_browser.get(f"{base_url}/admin/users")
    _wait_table_or_empty(admin_browser)

    rows = admin_browser.find_elements(By.CSS_SELECTOR, "tbody tr")
    if len(rows) >= 50:
        # Seeded environment: pagination should be visible instead.
        assert admin_browser.find_elements(
            By.XPATH, "//button[normalize-space()='Next']"
        )
        return
    next_buttons = admin_browser.find_elements(
        By.XPATH, "//button[normalize-space()='Next']"
    )
    assert next_buttons == []


def test_inventory_pagination_shows_when_over_limit(logged_in_browser, base_url):
    """With a seeded multi-page device set, pagination controls appear."""
    logged_in_browser.get(f"{base_url}/inventory")
    _wait_table_or_empty(logged_in_browser)

    # Seed creates hundreds of devices. In empty environments, skip.
    rows = logged_in_browser.find_elements(By.CSS_SELECTOR, "tbody tr")
    if len(rows) < 50:
        import pytest

        pytest.skip("inventory has <50 devices; pagination not exercised")

    next_btn = logged_in_browser.find_element(
        By.XPATH, "//button[normalize-space()='Next']"
    )
    prev_btn = logged_in_browser.find_element(
        By.XPATH, "//button[normalize-space()='Prev']"
    )
    assert next_btn.is_displayed()
    # On page 1, Prev is disabled.
    assert prev_btn.get_attribute("disabled") is not None


def test_inventory_pagination_next_advances_page(logged_in_browser, base_url):
    """Clicking Next increments the page indicator."""
    logged_in_browser.get(f"{base_url}/inventory")
    _wait_table_or_empty(logged_in_browser)

    rows = logged_in_browser.find_elements(By.CSS_SELECTOR, "tbody tr")
    if len(rows) < 50:
        import pytest

        pytest.skip("inventory has <50 devices; pagination not exercised")

    page_indicator = logged_in_browser.find_element(
        By.XPATH, "//*[starts-with(normalize-space(.), 'Page ') and contains(., ' of ')]"
    )
    before = page_indicator.text

    next_btn = logged_in_browser.find_element(
        By.XPATH, "//button[normalize-space()='Next']"
    )
    # Scroll into view so the click lands on the button rather than whatever
    # sticky header currently covers it, then JS-click as a fallback against
    # overlays the native click can hit.
    logged_in_browser.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", next_btn
    )
    logged_in_browser.execute_script("arguments[0].click();", next_btn)

    WebDriverWait(logged_in_browser, WAIT).until(
        lambda d: d.find_element(
            By.XPATH,
            "//*[starts-with(normalize-space(.), 'Page ') and contains(., ' of ')]",
        ).text
        != before
    )
    after = logged_in_browser.find_element(
        By.XPATH, "//*[starts-with(normalize-space(.), 'Page ') and contains(., ' of ')]"
    ).text
    assert after != before
