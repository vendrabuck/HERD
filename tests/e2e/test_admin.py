"""E2E tests for admin pages (requires admin login)."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _wait_for_page(driver, timeout=WAIT):
    """Wait for a page to load: table with data, or empty state text."""
    wait = WebDriverWait(driver, timeout)
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No ') and contains(text(), 'found')]",
            )
        )
    )


def test_users_page_loads(admin_browser, base_url):
    """Admin users page renders with a user table."""
    admin_browser.get(f"{base_url}/admin/users")
    wait = WebDriverWait(admin_browser, WAIT)

    wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
    # At minimum the superadmin user exists
    rows = admin_browser.find_elements(By.CSS_SELECTOR, "tbody tr")
    assert len(rows) > 0


def test_users_page_shows_roles(admin_browser, base_url):
    """Users table shows role information."""
    admin_browser.get(f"{base_url}/admin/users")
    wait = WebDriverWait(admin_browser, WAIT)

    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))
    page_text = admin_browser.find_element(By.TAG_NAME, "body").text
    assert "admin" in page_text.lower() or "user" in page_text.lower()


def test_groups_page_loads(admin_browser, base_url):
    """Admin groups page renders."""
    admin_browser.get(f"{base_url}/admin/groups")
    wait = WebDriverWait(admin_browser, WAIT)

    # "Not Grouped" default group is always seeded
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))


def test_connections_page_loads(admin_browser, base_url):
    """Admin connections page renders."""
    admin_browser.get(f"{base_url}/admin/connections")
    _wait_for_page(admin_browser)


def test_drivers_page_loads(admin_browser, base_url):
    """Admin drivers page renders."""
    admin_browser.get(f"{base_url}/admin/drivers")
    _wait_for_page(admin_browser)


def test_device_groups_page_loads(admin_browser, base_url):
    """Admin device groups page renders."""
    admin_browser.get(f"{base_url}/admin/device-groups")
    wait = WebDriverWait(admin_browser, WAIT)

    # "No Pool" default group is always seeded
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))


def test_templates_page_loads(admin_browser, base_url):
    """Templates page renders."""
    admin_browser.get(f"{base_url}/templates")
    _wait_for_page(admin_browser)
