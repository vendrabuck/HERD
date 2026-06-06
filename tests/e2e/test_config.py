"""E2E tests for the config service UI (/config, outside auth)."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def test_config_page_reachable_without_login(browser, base_url):
    """The /config route loads without requiring HERD auth."""
    browser.get(f"{base_url}/config")
    wait = WebDriverWait(browser, WAIT)
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//*[contains(text(), 'HERD Configuration')]"
                " | //*[contains(text(), 'Setup')]"
                " | //input[@id='config-password']",
            )
        )
    )
    # URL is not redirected to /login.
    assert "/login" not in browser.current_url


def test_config_login_form_renders(browser, base_url):
    """Config login form has a password input and sign-in button."""
    browser.get(f"{base_url}/config")
    wait = WebDriverWait(browser, WAIT)
    pw = wait.until(
        EC.presence_of_element_located((By.ID, "config-password"))
    )
    assert pw.is_displayed()
    btn = browser.find_element(
        By.XPATH, "//button[normalize-space()='Sign in']"
    )
    assert btn.is_displayed()


def test_config_wrong_password_shows_toast(browser, base_url):
    """Submitting with a clearly wrong password produces an error toast."""
    browser.get(f"{base_url}/config")
    wait = WebDriverWait(browser, WAIT)
    pw = wait.until(EC.presence_of_element_located((By.ID, "config-password")))
    pw.clear()
    pw.send_keys("not-the-real-password")
    browser.find_element(
        By.XPATH, "//button[normalize-space()='Sign in']"
    ).click()

    import time

    time.sleep(1.5)
    body = browser.find_element(By.TAG_NAME, "body").text.lower()
    assert "invalid" in body or "incorrect" in body or "password" in body
