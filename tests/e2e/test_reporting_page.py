"""E2E tests for the admin Reporting page (/reporting).

GAPS coverage: the Reporting nav item and ReportingPage had no e2e tests.
These pin the admin view: the "Utilization Report" heading, the three
headline stat cards, the range preset buttons (and the Custom preset
revealing two date pickers), the five rollup table cards, and the daily
trend section. Data volume is seed-dependent, so assertions target the
always-rendered chrome (headings, labels, buttons) rather than row counts.

The non-admin redirect off /reporting is covered in
test_register_and_roles.py, which owns the non-admin browser session.
"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def _open_reporting(driver, base_url):
    driver.get(f"{base_url}/reporting")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.XPATH, "//h2[normalize-space()='Utilization Report']"))
    )


def test_reporting_page_shows_headline_cards(admin_browser, base_url):
    """The report header and the three stat cards render for an admin."""
    _open_reporting(admin_browser, base_url)
    body = admin_browser.find_element(By.TAG_NAME, "body").text
    assert "Utilization Report" in body
    # The stat-card labels are uppercased via CSS text-transform, so Selenium's
    # rendered .text returns them in caps; compare case-insensitively.
    body_upper = body.upper()
    for label in ("Total reservation-hours", "Reservations counted", "Execution runs"):
        assert label.upper() in body_upper, f"stat card label missing: {label}"


def test_reporting_range_preset_buttons(admin_browser, base_url):
    """The 7 days / 30 days / Custom presets render; Custom reveals date inputs."""
    _open_reporting(admin_browser, base_url)
    wait = WebDriverWait(admin_browser, WAIT)

    for label in ("7 days", "30 days", "Custom"):
        btn = admin_browser.find_element(By.XPATH, f"//button[normalize-space()='{label}']")
        assert btn.is_displayed(), f"range preset button missing: {label}"

    admin_browser.find_element(By.XPATH, "//button[normalize-space()='Custom']").click()
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "input[type='date']")) == 2)

    # Switching back to a preset hides the pickers again.
    admin_browser.find_element(By.XPATH, "//button[normalize-space()='30 days']").click()
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "input[type='date']")) == 0)


def test_reporting_rollup_table_cards(admin_browser, base_url):
    """All five rollup cards and the daily trend section render."""
    _open_reporting(admin_browser, base_url)
    body = admin_browser.find_element(By.TAG_NAME, "body").text
    for title in (
        "By User",
        "By Device",
        "By Group (cost center)",
        "By Topology Type",
        "By Template",
        "Daily trend (reservation-hours)",
    ):
        assert title in body, f"rollup section missing: {title}"


def test_reporting_csv_buttons_render(admin_browser, base_url):
    """The Download CSV actions render on the exportable cards."""
    _open_reporting(admin_browser, base_url)
    buttons = admin_browser.find_elements(By.XPATH, "//button[normalize-space()='Download CSV']")
    # By User, By Device, and By Template each carry one.
    assert len(buttons) == 3
