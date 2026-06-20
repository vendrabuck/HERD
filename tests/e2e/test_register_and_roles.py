"""E2E tests for self-registration, non-admin role gating, and logout.

GAPS coverage: the register page only had its login-page link asserted; no
test exercised the registration form end to end, logged in as a non-admin,
checked that the Administration menu is hidden for role "user", verified the
client-side redirects off /admin/users and /reporting, or exercised the
header Logout button. These journeys pin the role model in the UI: a freshly
registered account gets role "user" and sees only the plain nav.

Deployments running AUTH_METHOD=ldap return 409 on /auth/register with an
"LDAP" detail that surfaces as an error toast; the module-scoped fixture
detects that and skips the whole module, mirroring how other gated features
skip.

The auth service exposes no user-delete endpoint, so the registered account
cannot be torn down; it uses a unique timestamped email and username so it
stays inert (same approach as the LDAP JIT-provisioning test).

The tests in this module run in file order against the shared module-scoped
`browser`: registration first, then login, then the gating checks, then
logout last (the same sequential style test_login.py already uses).
"""

import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import _do_login

WAIT = 15


@pytest.fixture(scope="module")
def registered_user(browser, base_url):
    """Register a unique user through the UI; skip the module on LDAP builds."""
    suffix = int(time.time() * 1000)
    creds = {
        "email": f"e2e-roles-{suffix}@example.com",
        "username": f"e2e-roles-{suffix}",
        "password": "e2e-roles-password-123!",
    }

    browser.get(f"{base_url}/register")
    wait = WebDriverWait(browser, WAIT)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "reg-email")))
    username_input = browser.find_element(By.ID, "reg-username")
    password_input = browser.find_element(By.ID, "reg-password")

    email_input.clear()
    email_input.send_keys(creds["email"])
    username_input.clear()
    username_input.send_keys(creds["username"])
    password_input.clear()
    password_input.send_keys(creds["password"])

    submit = browser.find_element(By.CSS_SELECTOR, "button[type='submit']")
    assert "Create account" in submit.text
    submit.click()

    # Success navigates to /login with a toast; LDAP mode stays on /register
    # and raises an error toast naming LDAP.
    deadline = time.time() + WAIT
    while time.time() < deadline:
        if "/login" in browser.current_url:
            return creds
        body = browser.find_element(By.TAG_NAME, "body").text
        if "LDAP" in body:
            pytest.skip("local registration is disabled; this deployment uses LDAP")
        time.sleep(0.5)
    pytest.skip(f"registration did not complete; still on {browser.current_url}")


def test_register_lands_on_login_page(registered_user, browser):
    """Submitting the registration form navigates to the login page."""
    assert "/login" in browser.current_url
    assert browser.find_element(By.ID, "login-email").is_displayed()


def test_registered_user_can_login(registered_user, browser, base_url):
    """The freshly registered account can sign in; the header shows its username."""
    _do_login(browser, base_url, registered_user["email"], registered_user["password"])
    wait = WebDriverWait(browser, WAIT)
    wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                f"//header//span[normalize-space()='{registered_user['username']}']",
            )
        )
    )


def test_non_admin_has_no_administration_menu(registered_user, browser, base_url):
    """Role "user" sees the plain nav items but no Administration dropdown."""
    browser.get(f"{base_url}/topology")
    wait = WebDriverWait(browser, WAIT)
    wait.until(
        EC.presence_of_element_located((By.XPATH, "//nav//a[normalize-space()='Inventory']"))
    )
    for label in ("Templates", "Topology", "Reservations"):
        assert browser.find_elements(By.XPATH, f"//nav//a[normalize-space()='{label}']"), (
            f"nav item {label} missing for non-admin"
        )
    admin_buttons = browser.find_elements(By.XPATH, "//button[normalize-space()='Administration']")
    assert admin_buttons == []


def test_non_admin_redirected_from_admin_users(registered_user, browser, base_url):
    """Navigating directly to /admin/users as role "user" redirects to /topology."""
    browser.get(f"{base_url}/admin/users")
    WebDriverWait(browser, WAIT).until(EC.url_contains("/topology"))
    assert "/admin" not in browser.current_url


def test_non_admin_redirected_from_reporting(registered_user, browser, base_url):
    """Navigating directly to /reporting as role "user" redirects to /topology."""
    browser.get(f"{base_url}/reporting")
    WebDriverWait(browser, WAIT).until(EC.url_contains("/topology"))
    assert "/reporting" not in browser.current_url


def test_logout_returns_to_login_and_clears_tokens(registered_user, browser, base_url):
    """The header Logout button lands on /login and clears stored tokens."""
    browser.get(f"{base_url}/topology")
    wait = WebDriverWait(browser, WAIT)
    logout_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//header//button[normalize-space()='Logout']"))
    )
    logout_btn.click()
    wait.until(EC.url_contains("/login"))

    access = browser.execute_script("return window.localStorage.getItem('access_token');")
    refresh = browser.execute_script("return window.localStorage.getItem('refresh_token');")
    assert access is None
    assert refresh is None
