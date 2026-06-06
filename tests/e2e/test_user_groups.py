"""E2E tests for user groups admin pages."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_groups(driver, base_url):
    driver.get(f"{base_url}/admin/groups")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "table"))
    )


def test_user_groups_list_has_create_button(admin_browser, base_url):
    """User groups list has the Create User Group button."""
    _open_groups(admin_browser, base_url)
    btn = admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create User Group')]"
    )
    assert btn.is_displayed()


def test_user_groups_shows_not_grouped(admin_browser, base_url):
    """The seeded 'Not Grouped' default group appears in the list."""
    _open_groups(admin_browser, base_url)
    body = admin_browser.find_element(By.TAG_NAME, "body").text
    assert "Not Grouped" in body


def test_user_groups_new_page_renders_form(admin_browser, base_url):
    """Clicking Create opens the new group form."""
    _open_groups(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create User Group')]"
    ).click()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.url_contains("/admin/groups/new"))
    wait.until(EC.presence_of_element_located((By.ID, "group-name")))


def test_user_groups_not_grouped_detail_renders(admin_browser, base_url):
    """Opening Not Grouped loads detail with the Members TransferList."""
    _open_groups(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//tr[td[normalize-space()='Not Grouped']]"
    ).click()

    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "group-name")))
    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Members')]")
        )
    )
    # Body uses CSS text-transform: uppercase, so match case-insensitively.
    body = admin_browser.find_element(By.TAG_NAME, "body").text.lower()
    assert "available users" in body or "available" in body
