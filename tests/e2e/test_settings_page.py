"""E2E tests for the Settings page notification preferences.

GAPS coverage: "Settings page (channel/event toggles round-trip; prefs visible
to backend)". We flip the in-app channel toggle and one event toggle, click
Save, then verify both the toast and that GET /api/notifications/preferences
returns the new values. To leave shared admin state clean, we restore the
original prefs in a fixture finalizer.
"""

import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15
PREFS_PATH = "/notifications/notifications/preferences"


@pytest.fixture
def restore_prefs(admin_browser):
    """Capture current notification preferences and restore them after the test."""
    admin_browser.get(admin_browser.current_url or "about:blank")
    resp = api_request(admin_browser, "GET", PREFS_PATH, allow_errors=True)
    if resp.status_code != 200:
        pytest.skip(f"prefs unavailable: {resp.status_code}")
    original = resp.json()
    yield original
    api_request(
        admin_browser,
        "PUT",
        PREFS_PATH,
        json={
            "channels": original.get("channels", {"in_app": True}),
            "events": original.get("events", {}),
        },
        allow_errors=True,
    )


def _open_settings(driver, base_url):
    driver.get(f"{base_url}/settings")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (By.XPATH, "//h1[normalize-space()='Settings']")
        )
    )


def test_settings_page_loads(admin_browser, base_url):
    """Settings page renders the Notifications section."""
    _open_settings(admin_browser, base_url)
    section = admin_browser.find_element(
        By.XPATH, "//h2[normalize-space()='Notifications']"
    )
    assert section.is_displayed()


def test_settings_shows_event_toggles(admin_browser, base_url):
    """Settings page shows the four reservation event toggles."""
    _open_settings(admin_browser, base_url)
    for label in (
        "Reservation confirmed",
        "Reservation updated",
        "Reservation cancelled",
        "Reservation completed",
    ):
        matches = admin_browser.find_elements(
            By.XPATH, f"//label[contains(., '{label}')]"
        )
        assert matches, f"missing toggle for '{label}'"


def test_settings_in_app_toggle_round_trip(admin_browser, base_url, restore_prefs):
    """Flipping the in-app channel toggle and saving persists the new value."""
    _open_settings(admin_browser, base_url)

    in_app_label = admin_browser.find_element(
        By.XPATH, "//label[contains(., 'Show in-app notifications')]"
    )
    in_app_checkbox = in_app_label.find_element(
        By.CSS_SELECTOR, "input[type='checkbox']"
    )
    before = in_app_checkbox.is_selected()
    in_app_checkbox.click()
    after = in_app_checkbox.is_selected()
    assert after != before, "checkbox click did not toggle state"

    save = admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Save']"
    )
    save.click()
    # toast appears
    WebDriverWait(admin_browser, WAIT).until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Preferences saved')]")
        )
    )

    # Backend reflects the change.
    time.sleep(0.5)
    resp = api_request(admin_browser, "GET", PREFS_PATH)
    assert resp.json()["channels"]["in_app"] is after


def test_settings_shows_outbound_channel_toggles(admin_browser, base_url):
    """Settings page shows the three outbound channel toggles (ROADMAP #40)."""
    _open_settings(admin_browser, base_url)
    for label in ("Email", "Chat", "Webhook"):
        matches = admin_browser.find_elements(
            By.XPATH, f"//label[contains(., '{label}')]"
        )
        assert matches, f"missing outbound channel toggle for '{label}'"


def test_settings_shows_expiring_soon_event_toggle(admin_browser, base_url):
    """Settings page shows the expiring-soon event toggle (ROADMAP #40)."""
    _open_settings(admin_browser, base_url)
    matches = admin_browser.find_elements(
        By.XPATH, "//label[contains(., 'Reservation expiring soon')]"
    )
    assert matches, "missing toggle for 'Reservation expiring soon'"


def test_settings_email_channel_round_trip(admin_browser, base_url, restore_prefs):
    """Enabling the email channel and saving persists channels.email (ROADMAP #40).

    Outbound channels default off, so this proves opt-in round-trips to the
    backend even though no SMTP transport is wired in the test stack.
    """
    _open_settings(admin_browser, base_url)

    email_label = admin_browser.find_element(
        By.XPATH, "//label[contains(., 'Email')]"
    )
    checkbox = email_label.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
    before = checkbox.is_selected()
    checkbox.click()
    after = checkbox.is_selected()
    assert after != before

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Save']"
    ).click()
    WebDriverWait(admin_browser, WAIT).until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Preferences saved')]")
        )
    )

    time.sleep(0.5)
    resp = api_request(admin_browser, "GET", PREFS_PATH)
    assert resp.json()["channels"]["email"] is after


def test_settings_event_toggle_round_trip(admin_browser, base_url, restore_prefs):
    """Toggling 'Reservation cancelled' off and saving persists to the backend."""
    _open_settings(admin_browser, base_url)

    label_el = admin_browser.find_element(
        By.XPATH, "//label[contains(., 'Reservation cancelled')]"
    )
    checkbox = label_el.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
    before = checkbox.is_selected()
    checkbox.click()
    after = checkbox.is_selected()
    assert after != before

    admin_browser.find_element(
        By.XPATH, "//button[normalize-space()='Save']"
    ).click()
    WebDriverWait(admin_browser, WAIT).until(
        EC.visibility_of_element_located(
            (By.XPATH, "//*[contains(text(), 'Preferences saved')]")
        )
    )

    time.sleep(0.5)
    resp = api_request(admin_browser, "GET", PREFS_PATH)
    events = resp.json().get("events", {})
    assert events.get("reservation.cancelled") is after
