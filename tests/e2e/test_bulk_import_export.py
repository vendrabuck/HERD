"""E2E tests for the bulk import/export UI on the admin list pages.

Verifies that an admin sees the export buttons and import trigger on the
inventory, templates, and topology pages, and that the import dialog opens with
its file picker, format select, and dry-run/import actions. Requires a running
stack with the frontend served (`make test-e2e`).
"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open(driver, base_url, path):
    driver.get(f"{base_url}{path}")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.XPATH, "//table | //*[contains(text(), 'No ')] | //h2"))
    )


def _export_import_buttons_present(driver):
    export_json = driver.find_elements(By.XPATH, "//button[normalize-space()='Export JSON']")
    export_csv = driver.find_elements(By.XPATH, "//button[normalize-space()='Export CSV']")
    import_btn = driver.find_elements(By.XPATH, "//button[normalize-space()='Import']")
    return export_json, export_csv, import_btn


def test_inventory_page_has_bulk_controls(admin_browser, base_url):
    _open(admin_browser, base_url, "/inventory")
    export_json, export_csv, import_btn = _export_import_buttons_present(admin_browser)
    assert export_json and export_json[0].is_displayed()
    assert export_csv and export_csv[0].is_displayed()
    assert import_btn and import_btn[0].is_displayed()


def test_templates_page_has_bulk_controls(admin_browser, base_url):
    _open(admin_browser, base_url, "/templates")
    export_json, export_csv, import_btn = _export_import_buttons_present(admin_browser)
    assert export_json and export_json[0].is_displayed()
    assert export_csv and export_csv[0].is_displayed()
    assert import_btn and import_btn[0].is_displayed()


def test_topology_page_has_bulk_controls(admin_browser, base_url):
    _open(admin_browser, base_url, "/topology")
    export_json, export_csv, import_btn = _export_import_buttons_present(admin_browser)
    assert export_json and export_json[0].is_displayed()
    assert import_btn and import_btn[0].is_displayed()


def test_import_dialog_opens_with_controls(admin_browser, base_url):
    _open(admin_browser, base_url, "/inventory")
    admin_browser.find_element(By.XPATH, "//button[normalize-space()='Import']").click()
    wait = WebDriverWait(admin_browser, WAIT)
    # File input, format select, and the dry-run + commit actions are present.
    wait.until(EC.presence_of_element_located((By.ID, "bulk-file")))
    assert admin_browser.find_element(By.ID, "bulk-format").is_displayed()
    assert admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Dry run']"
    ).is_displayed()
    assert admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Import now']"
    ).is_displayed()
