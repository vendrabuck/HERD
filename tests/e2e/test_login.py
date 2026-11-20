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


def test_login_page_has_author_credit(browser, base_url):
    """The author credit sits under the sign-in card, clear of it, and links to
    the author's LinkedIn profile in a new tab without an opener or referrer."""
    browser.get(f"{base_url}/login")
    wait = WebDriverWait(browser, WAIT)

    link = wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "[data-testid='author-credit'] a"))
    )
    credit = browser.find_element(By.CSS_SELECTOR, "[data-testid='author-credit']")
    assert " ".join(credit.text.split()) == "Designed and built by Lane Nelms"
    assert link.get_attribute("href") == "https://www.linkedin.com/in/lane-nelms/"
    assert link.get_attribute("target") == "_blank"
    assert link.get_attribute("rel") == "noopener noreferrer"
    # jsdom has no layout, so the no-overlap check lives here: the credit's
    # footer starts below the card it follows, and the page has no sideways scroll.
    card_bottom, footer_top, scroll_width, inner_width = browser.execute_script(
        "const f = document.querySelector(\"[data-testid='author-credit']\").closest('footer');"
        "const card = f.previousElementSibling.getBoundingClientRect();"
        "return [card.bottom, f.getBoundingClientRect().top,"
        " document.documentElement.scrollWidth, document.documentElement.clientWidth];"
    )
    assert footer_top >= card_bottom, (card_bottom, footer_top)
    assert scroll_width <= inner_width, (scroll_width, inner_width)


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
    error_xpath = "//*[contains(text(), 'Invalid credentials')]"
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
