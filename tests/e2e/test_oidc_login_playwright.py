"""Browser e2e for single sign-on through the OIDC test provider (ADR 0016 phase 5b).

D12 level 5. A real Chromium on the host signs in through the stack's login
page and Dex's real login form (infra/oidc-test): the "Sign in with Dex" button
in place of the password form, Dex's form, the callback and the one-time
handoff in the browser's own cookie jar (SameSite and Secure as a browser
applies them, which no HTTP-level suite exercises), landing on /topology signed
in as the provisioned account, logout, a cancelled sign-in, and a refused one.
The browser is `pw_oidc_browser` (tests/e2e/conftest.py), which maps the
issuer host `oidc-test` to the lab's published address.

Runs only in the Makefile's _gate-oidc-stack-tests phase (master, everything,
nightly), which recreates the gate's auth in OIDC mode and sets
HERD_E2E_OIDC=1. Every other e2e pass runs against a local-mode stack, so
without the variable the module skips, and the skip carries seeded_skip_ok so
the seeded pass's no-skip gate (HERD_E2E_REQUIRE_NO_SKIP=1) lists it as exempt
instead of failing. With the variable set, a stack that is not in OIDC mode
FAILS every test with the cause.

Teardown: the module deletes every account federated with the lab's issuer
before the first test and after the last, through the stack's Postgres (OIDC
mode has no API that deletes a user), as tests/integration/test_oidc_auth.py
does.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import (
    HOST_BASE_URL,
    pw_api,
    pw_login_form_was_inserted,
    pw_token,
    pw_watch_login_page,
)

ISSUER = "http://oidc-test:5556/dex"
PASSWORD = "oidc-test-password"
# The provider name _gate-oidc-stack-tests sets (OIDC_PROVIDER_NAME=Dex).
SIGN_IN_LABEL = "Sign in with Dex"
ALICE = "alice@oidc.example.com"
UNVERIFIED = "bob@oidc.example.com"
# frontend/src/lib/sso.ts, verbatim.
CANCELLED_MESSAGE = "Sign-in was cancelled."
FAILED_MESSAGE = "Single sign-on failed. If this continues, contact an administrator."

REPO_ROOT = Path(__file__).resolve().parents[2]
# The variable that selects this module; the gate phase sets it, and
# tests/unit/test_oidc_stack_gate_wiring.py pins that by reading this constant
# (and conftest.py's OIDC_TEST_HOST_ENV).
SELECTOR_ENV = "HERD_E2E_OIDC"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get(SELECTOR_ENV) != "1",
        reason=f"{SELECTOR_ENV} not set; the stack is not configured for OIDC auth.",
    ),
    pytest.mark.seeded_skip_ok(
        "gate stack runs AUTH_METHOD=local; the OIDC-mode stack phase runs this file "
        "with HERD_E2E_OIDC=1"
    ),
]


def _psql(sql: str) -> str:
    """One statement in the stack's Postgres (bare `docker compose`, so
    COMPOSE_PROJECT_NAME picks the project, as tests/integration/conftest.py's
    _psql does); its tuples-only output, or a failure."""
    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        os.environ.get("POSTGRES_USER", "herd"),
        "-d",
        os.environ.get("POSTGRES_DB", "herd"),
        "-tA",
        "-c",
        sql,
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.fail(f"psql failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _delete_federated_accounts() -> None:
    _psql(
        "DELETE FROM auth.users WHERE id IN (SELECT user_id FROM auth.federated_identities "
        f"WHERE issuer = '{ISSUER}')"
    )


@pytest.fixture(scope="module", autouse=True)
def _stack_in_oidc_mode():
    """Fail every test with the cause when the stack is not in OIDC mode; delete
    the lab's federated accounts before the first test and after the last."""
    try:
        with httpx.Client(verify=False, timeout=15.0) as client:
            resp = client.get(f"{HOST_BASE_URL}/api/auth/login-options")
    except httpx.HTTPError as exc:
        pytest.fail(f"the stack at {HOST_BASE_URL} does not answer ({type(exc).__name__})")
    mode = resp.json().get("auth_method") if resp.status_code == 200 else None
    if mode != "oidc":
        pytest.fail(
            f"{SELECTOR_ENV}=1 but GET /api/auth/login-options answers {resp.status_code} with "
            f"auth_method {mode!r}, not 'oidc'; the gate phase recreates auth in OIDC mode."
        )
    _delete_federated_accounts()
    yield
    _delete_federated_accounts()


def _is_exchange(response) -> bool:
    return response.request.method == "POST" and response.url.split("?", 1)[0].endswith(
        "/api/auth/oidc/exchange"
    )


def _is_logout(response) -> bool:
    return response.request.method == "POST" and response.url.split("?", 1)[0].endswith(
        "/api/auth/logout"
    )


def _open_login_page(page):
    """The login page in OIDC mode, once the login-options answer has rendered:
    the button, and neither the password form, its toggle, nor Register, which
    were never inserted at all, from the first byte of the document (issue
    #1420: the page renders no sign-in method while the answer is pending)."""
    pw_watch_login_page(page)
    page.goto(f"{HOST_BASE_URL}/login")
    button = page.get_by_role("button", name=SIGN_IN_LABEL, exact=True)
    expect(button).to_be_visible()
    expect(page.locator("#login-email")).to_have_count(0)
    expect(page.get_by_role("button", name="Administrator sign-in", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Register", exact=True)).to_have_count(0)
    assert pw_login_form_was_inserted(page) is False, (
        "the password form or the Register link was inserted before the OIDC answer rendered"
    )
    return button


def _submit_dex_form(page, email: str) -> None:
    """Fill and submit Dex's own login form (the page the button led to)."""
    login = page.locator("input[name='login']")
    expect(login).to_be_visible()
    assert page.url.startswith(f"{ISSUER}/auth/"), page.url
    login.fill(email)
    page.locator("input[name='password']").fill(PASSWORD)
    page.locator("button[type='submit']").click()


def test_sign_in_with_dex_lands_signed_in_and_logout_returns_to_the_button(pw_oidc_page):
    page = pw_oidc_page
    _open_login_page(page).click()
    with page.expect_response(_is_exchange) as exchange:
        _submit_dex_form(page, ALICE)
    assert exchange.value.status == 200
    # /login/sso moves to /topology with a client-side navigation, so no
    # document load belongs to it (pw_login's reason for "commit").
    page.wait_for_url("**/topology**", wait_until="commit")

    me = pw_api(page, "GET", "/auth/me").json()
    assert (me["email"], me["username"], me["full_name"], me["role"]) == (
        ALICE,
        "alice.sso",
        "Alice Example",
        "user",
    )
    expect(page.locator("header span[title='alice.sso']")).to_have_text("Alice Example")

    with page.expect_response(_is_logout) as logout:
        page.get_by_role("button", name="Logout", exact=True).click()
    assert logout.value.status == 204
    page.wait_for_url("**/login", wait_until="commit")
    expect(page.get_by_role("button", name=SIGN_IN_LABEL, exact=True)).to_be_visible()
    assert pw_token(page) is None


def test_a_cancelled_sign_in_shows_the_cancelled_message(pw_oidc_page):
    """The provider answering `error=access_denied` (a user who cancels at the
    IdP). The lab's Dex skips its approval screen, so it has no cancel button;
    the browser is sent to the callback with Dex's error answer for the state
    of the flow it began, carrying its own binding cookie, as Dex would."""
    page = pw_oidc_page
    button = _open_login_page(page)
    with page.expect_request(lambda r: r.url.startswith(f"{ISSUER}/auth?")) as authorize:
        button.click()
    state = parse_qs(urlsplit(authorize.value.url).query)["state"][0]
    expect(page.locator("input[name='login']")).to_be_visible()

    page.goto(f"{HOST_BASE_URL}/api/auth/oidc/callback?error=access_denied&state={state}")
    expect(page.get_by_text(CANCELLED_MESSAGE, exact=True)).to_be_visible()
    # The page removes sso_error with a replace navigation once the toast shows.
    expect(page).to_have_url(re.compile(r"/login$"))
    assert pw_token(page) is None


def test_a_refused_provider_user_sees_the_failed_message(pw_oidc_page):
    """bob's email is unverified at the provider, so auth refuses the first
    sign-in (ADR 0016 D4) and sends the browser to the failed message."""
    page = pw_oidc_page
    _open_login_page(page).click()
    _submit_dex_form(page, UNVERIFIED)
    expect(page.get_by_text(FAILED_MESSAGE, exact=True)).to_be_visible()
    expect(page).to_have_url(re.compile(r"/login$"))
    assert pw_token(page) is None
    assert _psql(f"SELECT count(*) FROM auth.users WHERE email = '{UNVERIFIED}'") == "0"
