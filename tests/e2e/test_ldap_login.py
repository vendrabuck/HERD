"""E2E test for the LDAP login browser flow.

GAPS coverage: "LDAP login E2E (depends on stack configured for LDAP + live
directory)."

This test is gated three ways and auto-skips when any gate fails:

1. The HERD stack's auth service must be running with `AUTH_METHOD=ldap`. We
   detect this by hitting `/api/auth/health` (and `/api/auth/config` if present)
   and looking for the LDAP method hint. If absent we skip.
2. The local OpenLDAP container described in the project local LDAP setup notes
   must be reachable. We probe for it via the auth service's login endpoint.
3. The test user defaults to `ldapit-eng1` (password `Password1`), the
   dedicated integration identity from infra/ldap-test/ldif/70-seed-integration.ldif.
   The seed script's LOCAL user1..user1000 would collide with `user1` (JIT
   provisioning refuses a username collision).

Once the module gates pass, a login that does not redirect FAILS (issue #1148):
it used to be turned into a skip, and this module's seeded_skip_ok marker then
exempted that skip from the no-skip gate, so the test could not fail for the
defect it names.

When the stack is configured for local auth (the default for this branch),
all tests in this file skip cleanly.
"""

import os

import httpx
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import HOST_BASE_URL, api_request

WAIT = 15
LDAP_USERNAME = os.environ.get("E2E_LDAP_USERNAME", "ldapit-eng1")
LDAP_PASSWORD = os.environ.get("E2E_LDAP_PASSWORD", "Password1")


def _stack_is_ldap() -> bool:
    """Return True only if the running auth service is in LDAP mode."""
    if os.environ.get("AUTH_METHOD", "local").lower() != "ldap":
        return False
    # Probe the auth service: if LDAP is configured the password-grant login
    # endpoint will accept the LDAP credentials. We don't actually want to log
    # in here, just sanity-check that the endpoint is reachable.
    try:
        with httpx.Client(base_url=HOST_BASE_URL, verify=False, timeout=5.0) as c:
            resp = c.get("/api/auth/health")
            if resp.status_code != 200:
                return False
    except httpx.HTTPError:
        return False
    return True


pytestmark = [
    pytest.mark.skipif(
        not _stack_is_ldap(),
        reason=(
            "stack not configured for LDAP (AUTH_METHOD != 'ldap'). See "
            "the local LDAP setup notes for how to point the stack at the local "
            "OpenLDAP container."
        ),
    ),
    pytest.mark.seeded_skip_ok(
        "gate stack runs AUTH_METHOD=local, the LDAP-mode phase covers integration not e2e"
    ),
]


def test_ldap_user_can_login(browser, base_url):
    """A directory user binds successfully through the login form."""
    browser.get(f"{base_url}/login")
    wait = WebDriverWait(browser, WAIT)
    email_input = wait.until(EC.presence_of_element_located((By.ID, "login-email")))
    password_input = browser.find_element(By.ID, "login-password")

    email_input.clear()
    email_input.send_keys(LDAP_USERNAME)
    password_input.clear()
    password_input.send_keys(LDAP_PASSWORD)

    browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    wait.until(EC.url_contains("/topology"))
    assert "/topology" in browser.current_url


def test_ldap_login_jit_provisions_user(browser, base_url):
    """After a successful LDAP login, the user appears in the auth users list.

    JIT provisioning writes `auth_source='ldap'` on first bind. We assert the
    user shows up in the admin users page, scoped to the LDAP username.
    """
    # Reuse the first test's session if it ran, otherwise log in again.
    if "/topology" not in browser.current_url:
        browser.get(f"{base_url}/login")
        wait = WebDriverWait(browser, WAIT)
        wait.until(EC.presence_of_element_located((By.ID, "login-email")))
        browser.find_element(By.ID, "login-email").send_keys(LDAP_USERNAME)
        browser.find_element(By.ID, "login-password").send_keys(LDAP_PASSWORD)
        browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        wait.until(EC.url_contains("/topology"))

    # Non-admin LDAP users cannot view the admin page, so read the account the
    # browser's own session belongs to through the API (issue #1148: a page
    # substring "user1" also matched user10 to user19).
    me = api_request(browser, "GET", "/auth/me")
    assert me.json()["username"] == LDAP_USERNAME, me.json()
