"""Playwright e2e for issue #1316: a refresh in one tab never signs out another.

Auth rotates the refresh token on every use and refuses a used one with no
grace window (IAM-SESSION-2), and every tab of a browser profile shares the
two localStorage keys. Before the fix each tab read the tokens once, at load:
tab B refreshed with the token tab A had already rotated, got a 401, and
cleared the tokens both tabs share.

Two pages in ONE browser context are two tabs of one profile: they share
localStorage, receive each other's `storage` events, and share Web Locks.
The access token lives 30 minutes, too long to wait for, so each page's next
API GET is answered once with a fabricated 401 through `page.route`; the
refresh that follows is real. Tab A refreshes first; tab B then gets its 401
WITHOUT a reload (a client-side navigation), so it acts on whatever its own
tab state is at that moment, exactly as a user's second tab would.

The effect is read back from the backend: the refresh token both tabs started
with is refused by auth afterwards (the rotation really happened), and each
tab's stored access token answers `GET /api/auth/me`. Nothing is mutated
beyond the admin's refresh-token rows, which auth owns and expires.
"""

import re

import httpx
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

WAIT_MS = 15000

_PASS_THROUGH = ("/api/auth/refresh", "/api/auth/login", "/api/auth/logout")


def _answer_next_api_get_with_401(page) -> None:
    """Fabricate a 401 for this page's next API GET (auth's own routes excluded), once."""
    state = {"used": False}

    def handler(route):
        request = route.request
        if (
            not state["used"]
            and request.method == "GET"
            and not any(part in request.url for part in _PASS_THROUGH)
        ):
            state["used"] = True
            route.fulfill(
                status=401,
                content_type="application/json",
                body='{"detail": "Could not validate credentials"}',
            )
            return
        route.continue_()

    page.route("**/api/**", handler)


def _is_refresh_post(response) -> bool:
    return response.request.method == "POST" and response.url.split("?", 1)[0].endswith(
        "/api/auth/refresh"
    )


def _stored_tokens(page) -> tuple[str | None, str | None]:
    return tuple(
        page.evaluate(
            "() => [localStorage.getItem('access_token'), localStorage.getItem('refresh_token')]"
        )
    )


def test_refresh_in_one_tab_keeps_the_other_tab_signed_in(pw_contexts):
    context = pw_contexts.new(ignore_https_errors=True)
    page_a = context.new_page()
    pw_login(page_a)
    page_b = context.new_page()
    page_b.goto(f"{HOST_BASE_URL}/inventory")
    expect(page_b).to_have_url(re.compile(r"/inventory"), timeout=WAIT_MS)
    _access_1, refresh_1 = _stored_tokens(page_b)
    assert refresh_1, "page B should share page A's stored session"

    # Tab A: its next API GET is refused, so it refreshes the shared token.
    _answer_next_api_get_with_401(page_a)
    with page_a.expect_response(_is_refresh_post, timeout=WAIT_MS) as refresh_a:
        page_a.reload()
    assert refresh_a.value.status == 200, refresh_a.value.text()
    expect(page_a).not_to_have_url(re.compile(r"/login"))
    access_2, refresh_2 = _stored_tokens(page_a)
    assert refresh_2 and refresh_2 != refresh_1

    # The rotation is real: auth now refuses the token both tabs started with.
    with httpx.Client(verify=False, timeout=30.0) as client:
        stale = client.post(f"{HOST_BASE_URL}/api/auth/refresh", json={"refresh_token": refresh_1})
    assert stale.status_code == 401, stale.text

    # Tab B acts without a reload, and its next API GET is refused too.
    _answer_next_api_get_with_401(page_b)
    with page_b.expect_response(_is_refresh_post, timeout=WAIT_MS) as refresh_b:
        page_b.get_by_role("link", name="Topology", exact=True).click()
    assert refresh_b.value.status == 200, refresh_b.value.text()
    expect(page_b).to_have_url(re.compile(r"/topology"), timeout=WAIT_MS)
    expect(page_b).not_to_have_url(re.compile(r"/login"))

    # B refreshed with the token A rotated to, never the stale one.
    sent = refresh_b.value.request.post_data_json
    assert sent == {"refresh_token": refresh_2}, sent

    # Both tabs hold one live session, read back from the backend.
    access_3, refresh_3 = _stored_tokens(page_b)
    assert refresh_3 and refresh_3 not in (refresh_1, refresh_2)
    assert _stored_tokens(page_a) == (access_3, refresh_3)
    assert pw_api(page_b, "GET", "/auth/me").status_code == 200
    page_a.reload()
    expect(page_a).not_to_have_url(re.compile(r"/login"), timeout=WAIT_MS)
    assert pw_api(page_a, "GET", "/auth/me").status_code == 200
    assert access_2 != access_3
