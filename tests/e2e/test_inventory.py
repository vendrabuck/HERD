"""E2E tests for the inventory page."""

import time

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15


def test_inventory_page_loads(logged_in_browser, base_url):
    """Inventory page renders (table or empty state)."""
    logged_in_browser.get(f"{base_url}/inventory")
    wait = WebDriverWait(logged_in_browser, WAIT)

    # Wait for either device table or "No devices found"
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No devices')]",
            )
        )
    )


def test_inventory_has_search_input(logged_in_browser, base_url):
    """Inventory page has a search input for filtering devices."""
    logged_in_browser.get(f"{base_url}/inventory")
    wait = WebDriverWait(logged_in_browser, WAIT)

    search_input = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder*='Search']"))
    )
    assert search_input.is_displayed()


def _read_inventory_filter(driver) -> dict:
    """The admin's saved inventory filter, or {} when the read fails or none is saved."""
    resp = api_request(driver, "GET", "/user-profile/preferences", allow_errors=True)
    if resp.status_code != 200:
        return {}
    return (resp.json().get("saved_filters") or {}).get("inventory") or {}


def _restore_inventory_filter(driver, base_url, baseline: dict | None) -> None:
    """Write the baseline back last, then fail the test if the read-back differs.

    `baseline` is None when it was never read, in which case nothing was changed
    and nothing is written. The page is reloaded at the app root first: a full
    navigation drops any search debounce still pending on the inventory page (the
    store has no unload flush), so no write from that page can land after the
    restore, and the JWT stays readable from the app origin's localStorage.
    """
    if baseline is None:
        return
    try:
        driver.get(f"{base_url}/")
    except WebDriverException:
        pass
    resp = api_request(
        driver,
        "PATCH",
        "/user-profile/preferences",
        json={"saved_filters": {"inventory": baseline}},
        allow_errors=True,
    )
    if resp.status_code >= 300:
        pytest.fail(
            f"inventory preference restore answered {resp.status_code}; baseline {baseline!r}"
        )
    restored = _read_inventory_filter(driver)
    if restored != baseline:
        pytest.fail(f"inventory preference not restored: {restored!r} != {baseline!r}")


def test_inventory_search_filters_results(logged_in_browser, base_url):
    """Typing in search updates the device list.

    The search box persists to the admin's shared preferences, so the test reads
    the saved inventory filter first and restores it in a finally block with a
    read-back (issue #1070 follow-up), as the Playwright inventory tests do.
    """
    baseline: dict | None = None
    try:
        baseline = _read_inventory_filter(logged_in_browser)

        logged_in_browser.get(f"{base_url}/inventory")
        wait = WebDriverWait(logged_in_browser, WAIT)

        # Wait for page to load
        wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//table | //*[contains(text(), 'No devices')]",
                )
            )
        )

        # The filtered empty state is what the search produces (issue #1148: the
        # typed term lives in the input's value, which WebElement.text never
        # includes, so "the term is not on the page" was always true). It must
        # not be on screen before typing, or a stale saved filter produced it.
        no_match = (
            By.XPATH,
            "//tbody//td[contains(normalize-space(), 'No devices match the current filters.')]",
        )
        assert logged_in_browser.find_elements(*no_match) == [], (
            "the filtered empty state was already showing before the search; "
            "a saved inventory filter was not restored"
        )

        search_input = logged_in_browser.find_element(
            By.CSS_SELECTOR, "input[placeholder*='Search']"
        )
        search_input.clear()
        typed = "nonexistent-device-xyz"
        search_input.send_keys(typed)

        wait.until(EC.presence_of_element_located(no_match))
        rows = logged_in_browser.find_elements(
            By.CSS_SELECTOR, "tbody tr button[aria-label*='Expand']"
        )
        assert rows == [], "a device row is still listed under a search no device name contains"

        # Erase via keystrokes so React's controlled onChange fires and the
        # debounced setSavedFilter writes an empty search back to user-profile.
        # element.clear() alone bypasses onChange, leaving the typed search saved.
        search_input.send_keys(Keys.END)
        search_input.send_keys(Keys.BACKSPACE * len(typed))
        # The empty search reaches the server about 500 ms after the last keystroke
        # (300 ms search debounce plus the store's 200 ms PATCH debounce). Poll the
        # saved preference until it reads back empty: that proves the page's own
        # final PATCH has landed, so the restore in the finally block is the last
        # write (issue #1070).
        deadline = time.monotonic() + WAIT
        saved = None
        while time.monotonic() < deadline:
            prefs = api_request(logged_in_browser, "GET", "/user-profile/preferences").json()
            saved = ((prefs.get("saved_filters") or {}).get("inventory") or {}).get("search") or ""
            if saved == "":
                break
            time.sleep(0.25)
        assert saved == "", f"inventory search still saved as {saved!r} after erasing it"
    finally:
        _restore_inventory_filter(logged_in_browser, base_url, baseline)
