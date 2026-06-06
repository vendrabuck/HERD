"""E2E tests for the login page."""

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 15


def test_login_page_renders(browser, base_url):
    """Login page shows email, password, and submit button."""
    browser.get(f"{base_url}/login")
    wait = WebDriverWait(browser, WAIT)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "login-email")))
    password_input = browser.find_element(By.ID, "login-password")
    submit_btn = browser.find_element(By.CSS_SELECTOR, "button[type='submit']")

    assert email_input.is_displayed()
    assert password_input.is_displayed()
    assert submit_btn.is_displayed()
    assert "Sign in" in submit_btn.text


def test_login_page_has_register_link(browser, base_url):
    """Login page has a link to the registration page."""
    browser.get(f"{base_url}/login")
    wait = WebDriverWait(browser, WAIT)

    register_link = wait.until(EC.presence_of_element_located((By.LINK_TEXT, "Register")))
    assert register_link.is_displayed()
    assert "/register" in register_link.get_attribute("href")


def test_login_failure_shows_error(browser, base_url):
    """Invalid credentials show an error toast."""
    browser.get(f"{base_url}/login")
    wait = WebDriverWait(browser, WAIT)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "login-email")))
    password_input = browser.find_element(By.ID, "login-password")

    email_input.clear()
    email_input.send_keys("nonexistent@example.com")
    password_input.clear()
    password_input.send_keys("wrongpassword")

    submit = browser.find_element(By.CSS_SELECTOR, "button[type='submit']")
    submit.click()

    # Wait for error toast to appear (use visibility, not just presence)
    error_xpath = "//*[contains(text(), 'Invalid email or password')]"
    wait.until(EC.visibility_of_element_located((By.XPATH, error_xpath)))


def test_login_success_redirects(browser, base_url, admin_credentials):
    """Valid credentials redirect to the topology page."""
    email, password = admin_credentials
    browser.get(f"{base_url}/login")
    wait = WebDriverWait(browser, WAIT)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "login-email")))
    password_input = browser.find_element(By.ID, "login-password")

    email_input.clear()
    email_input.send_keys(email)
    password_input.clear()
    password_input.send_keys(password)

    submit = browser.find_element(By.CSS_SELECTOR, "button[type='submit']")
    submit.click()

    wait.until(EC.url_contains("/topology"))
    assert "/topology" in browser.current_url
