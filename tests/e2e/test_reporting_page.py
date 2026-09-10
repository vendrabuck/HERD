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
        "Fleet Utilization",
        "Daily trend (reservation-hours)",
    ):
        assert title in body, f"rollup section missing: {title}"


# Exportable cards, keyed by the h3 title each CardHeader renders. The purpose
# cards (issue #646, PR #696) mount only after the report query resolves, so a
# count taken right after the heading appears races the data load: it sees the
# four always-rendered card buttons on a slow stack (nightly CI) and all seven
# on a fast one (the local make-everything gate). The test therefore waits for
# a data-only element before counting, and scopes each count to its card so a
# new exportable card changes this list on purpose instead of drifting a total.
EXPORTABLE_CARD_TITLES = (
    "By User",
    "By Device",
    "By Template",
    "Fleet Utilization",
    "Device-hours by purpose",
    "Purpose Mix - By User",
    "Purpose Mix - By Device",
)
NON_EXPORTABLE_CARD_TITLES = ("By Group (cost center)", "By Topology Type")
CSV_BUTTON = "button[normalize-space()='Download CSV']"


def _card_buttons(driver, title, button_xpath=CSV_BUTTON):
    """Buttons inside the header div whose h3 reads exactly `title`."""
    return driver.find_elements(
        By.XPATH, f"//h3[normalize-space()='{title}']/parent::div//{button_xpath}"
    )


def test_reporting_csv_buttons_render(admin_browser, base_url):
    """Each exportable card carries exactly one Download CSV action once the
    report has loaded; the non-exportable rollups carry none."""
    _open_reporting(admin_browser, base_url)
    # The purpose section exists only after the report query resolves, so its
    # heading is the load signal; the heading alone is not.
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located(
            (By.XPATH, "//h3[normalize-space()='Device-hours by purpose']")
        )
    )

    for title in EXPORTABLE_CARD_TITLES:
        buttons = _card_buttons(admin_browser, title)
        assert len(buttons) == 1, f"{title}: expected 1 Download CSV button, found {len(buttons)}"
        assert buttons[0].is_displayed(), f"{title}: Download CSV button not displayed"

    for title in NON_EXPORTABLE_CARD_TITLES:
        assert not _card_buttons(admin_browser, title), f"{title}: unexpected Download CSV button"

    # The suggested-bucket export sits beside the chart's main export under a
    # distinct label, so the exact-match count above does not include it.
    suggested = _card_buttons(
        admin_browser,
        "Device-hours by purpose",
        "button[normalize-space()='Download CSV (suggested)']",
    )
    assert len(suggested) == 1

    # Page-wide total pins the exportable set: nothing outside the named cards.
    all_buttons = admin_browser.find_elements(By.XPATH, f"//{CSV_BUTTON}")
    assert len(all_buttons) == len(EXPORTABLE_CARD_TITLES)
