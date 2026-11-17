"""The login page while GET /api/auth/login-options is pending (issue #1420), in a real browser.

The page renders no sign-in method until the server has said which apply: no
password form, no single sign-on button, no Register link, only a role=status
region ("Loading sign-in options...") with placeholder bars. A request that
never answers is aborted by the client after 5 s (LOGIN_OPTIONS_TIMEOUT_MS in
frontend/src/api/auth.ts) and the page falls back to today's page, the
password form (ADR 0016 D10), under a role=alert notice ("Sign-in options
could not be loaded.") whose Try again button asks again.

Each test holds the page's login-options GET in a Playwright route, the
pattern of test_inventory_saved_search_late_load_playwright.py, so the pending
state is asserted while the request is provably in flight, not by timing. A
held request is aborted by the page after 5 s, so a test that releases one
asserts the pending state with short timeouts and checks, before releasing,
that it is still inside that window (RELEASE_WITHIN_S); a slow runner then
fails with that cause instead of a confusing fallback.
These run against the local-mode gate and nightly stacks; the real OIDC-mode
page is covered by test_oidc_login_playwright.py in the OIDC stack phase.
Nothing is created, so there is nothing to tear down.
"""

import re
import time

from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login_form_was_inserted, pw_watch_login_page

OPTIONS_GLOB = "**/api/auth/login-options"
PENDING_TEXT = "Loading sign-in options..."
# The frontend's client limit on the request is 5 s; this leaves room for the
# fallback render on a loaded runner.
FALLBACK_WAIT_MS = 15_000
# A held request must be released well inside the client's 5 s limit.
RELEASE_WITHIN_S = 4.0
# Each pending-state check is immediate when the page is right; a short bound
# keeps a wrong page from eating the release window.
PENDING_CHECK_MS = 1_000
FAILED_TEXT = "Sign-in options could not be loaded."
OIDC_ANSWER = {
    "auth_method": "oidc",
    "registration_open": False,
    "password_login": False,
    "sso": {"provider_name": "Example SSO"},
}


def _hold_options(page) -> list:
    """Hold every GET of login-options the page sends; return the held routes,
    each as a (route, monotonic time it was held) pair."""
    held: list = []

    def hold(route):
        if route.request.method == "GET":
            held.append((route, time.monotonic()))
        else:
            route.continue_()

    page.route(OPTIONS_GLOB, hold)
    return held


def _wait_until_held(page, held: list, count: int = 1) -> None:
    """Wait for the page's `count`-th login-options GET to reach the route handler.

    Polls for up to five seconds."""
    for _ in range(100):
        if len(held) >= count:
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"login-options GET number {count} was not held")


def _release(held_entry, **fulfill) -> None:
    """Release a held GET (continue to the stack, or fulfill), failing first if
    the client's 5 s limit may already have aborted it."""
    route, held_at = held_entry
    elapsed = time.monotonic() - held_at
    assert elapsed < RELEASE_WITHIN_S, (
        f"released {elapsed:.1f}s after the request was held; the page aborts it at 5 s, "
        "so the pending-state checks before the release took too long"
    )
    if fulfill:
        route.fulfill(**fulfill)
    else:
        route.continue_()


def _expect_no_sign_in_method(page) -> None:
    t = PENDING_CHECK_MS
    expect(page.get_by_role("status").filter(has_text=PENDING_TEXT)).to_have_count(1, timeout=t)
    expect(page.locator("#login-email")).to_have_count(0, timeout=t)
    expect(page.locator("#login-password")).to_have_count(0, timeout=t)
    expect(page.get_by_role("link", name="Register", exact=True)).to_have_count(0, timeout=t)
    expect(page.get_by_role("button", name=re.compile(r"^Sign in with "))).to_have_count(
        0, timeout=t
    )
    expect(page.get_by_role("button", name="Administrator sign-in", exact=True)).to_have_count(
        0, timeout=t
    )
    expect(page.get_by_role("alert").filter(has_text=FAILED_TEXT)).to_have_count(0, timeout=t)


def test_no_sign_in_method_renders_until_login_options_answers(pw_page):
    page = pw_page
    pw_watch_login_page(page)
    held = _hold_options(page)
    page.goto(f"{HOST_BASE_URL}/login")
    _wait_until_held(page, held)
    _expect_no_sign_in_method(page)
    # The configuration link does not depend on the method.
    expect(page.get_by_role("link", name="System configuration", exact=True)).to_be_visible(
        timeout=PENDING_CHECK_MS
    )
    assert pw_login_form_was_inserted(page) is False

    _release(held[0])
    # The gate and nightly stacks run local mode: today's page.
    expect(page.locator("#login-email")).to_be_visible()
    expect(page.locator("#login-password")).to_be_visible()
    expect(page.get_by_role("link", name="Register", exact=True)).to_be_visible()
    expect(page.get_by_role("status").filter(has_text=PENDING_TEXT)).to_have_count(0)
    # A confirmed answer never shows the failure notice.
    expect(page.get_by_role("alert").filter(has_text=FAILED_TEXT)).to_have_count(0)


def test_an_oidc_answer_never_lets_the_password_form_render(pw_page):
    """The answer is fulfilled in the browser, so this holds on a local-mode
    stack: from the first byte of the document to the rendered button, the
    password form and the Register link are never inserted."""
    page = pw_page
    pw_watch_login_page(page)
    held = _hold_options(page)
    page.goto(f"{HOST_BASE_URL}/login")
    _wait_until_held(page, held)
    _expect_no_sign_in_method(page)

    _release(held[0], json=OIDC_ANSWER)
    expect(page.get_by_role("button", name="Sign in with Example SSO", exact=True)).to_be_visible()
    expect(page.locator("#login-email")).to_have_count(0)
    expect(page.get_by_role("link", name="Register", exact=True)).to_have_count(0)
    assert pw_login_form_was_inserted(page) is False


def test_a_login_options_request_that_never_answers_falls_back_to_the_password_form(pw_page):
    """The first GET is never released: the client aborts it at 5 s and the page
    shows the failure notice above today's page. Try again sends a second GET,
    held too, so the pending state shows again; released to the stack, it
    renders the stack's answer (local mode on the gate and nightly stacks)
    with no notice."""
    page = pw_page
    held = _hold_options(page)
    page.goto(f"{HOST_BASE_URL}/login")
    _wait_until_held(page, held)
    _expect_no_sign_in_method(page)
    # Never released: the client aborts it and the page shows today's page.
    expect(page.locator("#login-email")).to_be_visible(timeout=FALLBACK_WAIT_MS)
    expect(page.locator("#login-password")).to_be_visible()
    expect(page.get_by_role("link", name="Register", exact=True)).to_be_visible()
    expect(page.get_by_role("status").filter(has_text=PENDING_TEXT)).to_have_count(0)
    notice = page.get_by_role("alert").filter(has_text=FAILED_TEXT)
    expect(notice).to_be_visible()

    notice.get_by_role("button", name="Try again", exact=True).click()
    _wait_until_held(page, held, count=2)
    _expect_no_sign_in_method(page)
    _release(held[1])
    expect(page.locator("#login-email")).to_be_visible()
    expect(page.get_by_role("link", name="Register", exact=True)).to_be_visible()
    expect(notice).to_have_count(0)
    expect(page.get_by_role("status").filter(has_text=PENDING_TEXT)).to_have_count(0)
