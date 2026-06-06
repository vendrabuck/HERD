"""E2E tests for the templates page and editor."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_templates(driver, base_url):
    driver.get(f"{base_url}/templates")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No templates')]",
            )
        )
    )


def test_templates_page_shows_type_filter(admin_browser, base_url):
    """Templates page has a type filter dropdown with All/Device/Port."""
    _open_templates(admin_browser, base_url)
    selects = admin_browser.find_elements(By.CSS_SELECTOR, "select")
    assert selects, "expected a type filter select"
    options = [o.text for o in selects[0].find_elements(By.TAG_NAME, "option")]
    for expected in ("All", "Device", "Port"):
        assert expected in options


def test_templates_create_button_visible_for_admin(admin_browser, base_url):
    """Admin sees the Create Template button."""
    _open_templates(admin_browser, base_url)
    btn = admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Template')]"
    )
    assert btn.is_displayed()


def test_template_editor_opens_with_name_type_fields(admin_browser, base_url):
    """Clicking Create Template navigates to the editor with name + type inputs."""
    _open_templates(admin_browser, base_url)
    admin_browser.find_element(
        By.XPATH, "//button[contains(., 'Create Template')]"
    ).click()

    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.url_contains("/templates/new"))
    wait.until(EC.presence_of_element_located((By.ID, "tmpl-name")))
    assert admin_browser.find_element(By.ID, "tmpl-type").is_displayed()


def test_template_editor_device_type_shows_driver_field(admin_browser, base_url):
    """When template type is 'device', the driver dropdown is visible."""
    admin_browser.get(f"{base_url}/templates/new")
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "tmpl-name")))
    driver_select = admin_browser.find_element(By.ID, "tmpl-driver")
    assert driver_select.is_displayed()


def test_template_editor_port_type_hides_driver_field(admin_browser, base_url):
    """Switching template type to 'port' hides the driver dropdown."""
    admin_browser.get(f"{base_url}/templates/new")
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "tmpl-type")))

    from selenium.webdriver.support.ui import Select

    Select(admin_browser.find_element(By.ID, "tmpl-type")).select_by_value("port")

    drivers = admin_browser.find_elements(By.ID, "tmpl-driver")
    assert drivers == [] or not drivers[0].is_displayed()
