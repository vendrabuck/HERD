"""Integration: OpenID Connect sign-in through the running stack (ADR 0016 phase 5b).

D12 level 3. The gate's auth container runs in OIDC mode against the checked-in
Dex (infra/oidc-test), recreated that way by the Makefile's
_gate-oidc-stack-tests phase (master, everything, nightly), and every request
here goes through the gateway: `GET /api/auth/oidc/login`, Dex's own login form
driven over HTTP (no browser), `GET /api/auth/oidc/callback`, and
`POST /api/auth/oidc/exchange`, then the minted HERD tokens against auth and
against another service. What it proves beyond the live service suite of phase
5a (services/auth/tests/test_oidc_service_live.py, which runs the auth app over
ASGI): the compose wiring of the OIDC settings, the auth container reaching the
issuer by the alias `oidc-test` on the stack network, the gateway's routing and
prefix for the three routes, the cookies' Path through the gateway, and the
mode's refusals on the real stack.

Skipped unless HERD_INTEGRATION_OIDC=1 (which the gate phase sets), so the
ordinary `make test-integration` and nightly's integration pass, both against a
local-mode stack, never select it. With the variable set, a stack that is not
in OIDC mode, or a provider that does not answer, FAILS every test with the
cause instead of skipping.

Self-seeding and teardown (docs/specs/TESTING.md, Teardown contract): a
module-scoped autouse fixture deletes every account federated with the lab's
issuer, and any `oidc` account with a fixture email, before the first test and
after the last (the foreign keys of `auth.users` cascade or set NULL, so the
identity, flow, refresh-token, and group-membership rows go with it). The
collision test needs a LOCAL account holding the collision user's email
(user1@herd.dev, the seeded local user1's); the stack is not seeded at this
phase and OIDC mode refuses registration, so the fixture inserts that row
through Postgres when no account holds the email, and deletes it afterward only
if it inserted it. No API path exists for either step in OIDC mode (auth has no
admin create or delete of a user), the reason test_ldap_auth.py uses `_psql`.

Fixtures, from infra/oidc-test/dex.yaml (test-only values): alice and carol
(normal users), user1@herd.dev (the collision user), bob (`email_verified`
false); every password is `oidc-test-password`.
"""

from __future__ import annotations

import base64
import os
import re
import socket
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import bcrypt
import httpx
import pytest
from conftest import BASE_URL, SEED_EMAIL, SEED_PASSWORD, _psql

# The environment this suite reads; the gate phase sets exactly these, which
# tests/unit/test_oidc_stack_gate_wiring.py pins by reading these constants.
SELECTOR_ENV = "HERD_INTEGRATION_OIDC"
OIDC_HOST_ENV = "HERD_TEST_OIDC_HOST"
OIDC_PORT_ENV = "HERD_TEST_OIDC_PORT"

OIDC_HOST = os.getenv(OIDC_HOST_ENV, "127.0.0.1")
OIDC_PORT = int(os.getenv(OIDC_PORT_ENV, "5556"))
ISSUER_HOST = "oidc-test"
ISSUER_PORT = 5556
ISSUER = f"http://{ISSUER_HOST}:{ISSUER_PORT}/dex"
PASSWORD = "oidc-test-password"
# The provider name _gate-oidc-stack-tests sets (OIDC_PROVIDER_NAME=Dex).
PROVIDER_NAME = "Dex"

ALICE = "alice@oidc.example.com"
CAROL = "carol@oidc.example.com"
COLLIDER = "user1@herd.dev"
UNVERIFIED = "bob@oidc.example.com"
FIXTURE_EMAILS = (ALICE, CAROL, COLLIDER, UNVERIFIED)
ALICE_USER_ID = "6f1c5a0e-1b7e-4d0a-9a51-0c6b1f0e0001"
COLLIDER_USER_ID = "6f1c5a0e-1b7e-4d0a-9a51-0c6b1f0e0002"
UNVERIFIED_USER_ID = "6f1c5a0e-1b7e-4d0a-9a51-0c6b1f0e0003"
# The local account the collision fixture inserts when no account holds the
# collision email (never a username the seed script or Dex produces).
COLLIDER_LOCAL_USERNAME = "oidc-it-collider"

FLOW_COOKIE = "herd_oidc_flow"
HANDOFF_COOKIE = "herd_oidc_handoff"
SUCCESS_LOCATION = "/login/sso"
FAILED_LOCATION = "/login?sso_error=failed"
EXCHANGE_REFUSED_DETAIL = "Single sign-on session expired; sign in again"
REGISTER_DISABLED_DETAIL = "Local registration is disabled; this deployment uses single sign-on."

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv(SELECTOR_ENV) != "1",
        reason=f"{SELECTOR_ENV} not set; the stack is not configured for OIDC auth.",
    ),
]


def dex_subject(user_id: str, connector_id: str = "local") -> str:
    """The `sub` Dex issues for a static user: the unpadded base64url encoding of a
    protobuf message with the userID as field 1 and the connector id as field 2."""
    message = b"".join(
        bytes([tag, len(value)]) + value
        for tag, value in ((0x0A, user_id.encode()), (0x12, connector_id.encode()))
    )
    return base64.urlsafe_b64encode(message).rstrip(b"=").decode("ascii")


def _quote(value: str) -> str:
    """A SQL string literal (fixture values only; none holds a quote today)."""
    return "'" + value.replace("'", "''") + "'"


def _sql(statement: str) -> str:
    """Run one statement in the stack's Postgres; its tuples-only output, or a failure."""
    result = _psql(statement, tuples_only=True)
    if result.returncode != 0:
        pytest.fail(f"psql failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _delete_oidc_fixture_accounts() -> None:
    emails = ", ".join(_quote(e) for e in FIXTURE_EMAILS)
    _sql(
        "DELETE FROM auth.users WHERE id IN (SELECT user_id FROM auth.federated_identities "
        f"WHERE issuer = {_quote(ISSUER)}) OR (auth_source = 'oidc' AND email IN ({emails}))"
    )


@pytest.fixture(scope="module", autouse=True)
def _stack_in_oidc_mode():
    """Fail every test, with the cause, when the gate phase's preconditions are off."""
    try:
        with socket.create_connection((OIDC_HOST, OIDC_PORT), timeout=2.0):
            pass
    except OSError:
        pytest.fail(
            f"{SELECTOR_ENV}=1 but no OIDC provider answers at {OIDC_HOST}:{OIDC_PORT}; "
            "run this through `make _gate-oidc-stack-tests`, or `make oidc-up` first."
        )
    try:
        with httpx.Client(verify=False, timeout=15.0) as client:
            resp = client.get(f"{BASE_URL}/auth/login-options")
    except httpx.HTTPError as exc:
        pytest.fail(
            f"{SELECTOR_ENV}=1 but the stack at {BASE_URL} does not answer "
            f"({type(exc).__name__}); check HERD_BASE_URL."
        )
    mode = resp.json().get("auth_method") if resp.status_code == 200 else None
    if mode != "oidc":
        pytest.fail(
            f"{SELECTOR_ENV}=1 but GET /api/auth/login-options answers {resp.status_code} "
            f"with auth_method {mode!r}, not 'oidc'; the gate phase recreates auth in OIDC mode."
        )


@pytest.fixture(scope="module", autouse=True)
def _oidc_accounts_cleaned(_stack_in_oidc_mode):
    """Before the first test and after the last: no account from a fixture sign-in."""
    _delete_oidc_fixture_accounts()
    yield
    _delete_oidc_fixture_accounts()


@pytest.fixture(scope="module")
def local_collider(_oidc_accounts_cleaned):
    """The local account whose email the collision user signs in with; its id.

    Inserted only when no account holds the email (a seeded stack already has
    user1), and then deleted afterward; an existing account is left as found."""
    existing = _sql(f"SELECT id FROM auth.users WHERE email = {_quote(COLLIDER)}")
    if existing:
        yield existing
        return
    _sql(
        "INSERT INTO auth.users (id, email, username, full_name, hashed_password, "
        "auth_source, is_active, deactivated_by_sync, role) VALUES (gen_random_uuid(), "
        f"{_quote(COLLIDER)}, {_quote(COLLIDER_LOCAL_USERNAME)}, 'OIDC collision fixture', "
        "NULL, 'local', true, false, 'USER')"
    )
    inserted = _sql(f"SELECT id FROM auth.users WHERE email = {_quote(COLLIDER)}")
    assert inserted, "the collision fixture's local account was not inserted"
    try:
        yield inserted
    finally:
        _sql(f"DELETE FROM auth.users WHERE id = {_quote(inserted)} AND auth_source = 'local'")


# --- driving the sign-in over HTTP, as a browser would ---


class _MappedTransport(httpx.AsyncHTTPTransport):
    """Sends a request for `oidc-test:5556` to the published address instead (the
    5a live suite's transport; the Host header still names the issuer)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == ISSUER_HOST and request.url.port == ISSUER_PORT:
            request.url = request.url.copy_with(host=OIDC_HOST, port=OIDC_PORT)
        return await super().handle_async_request(request)


async def dex_sign_in(authorization_url: str, email: str) -> str:
    """Follow `authorization_url` through Dex's login form as `email`; the URL Dex
    redirects the browser to (the stack's callback with `code` and `state`)."""
    redirect_uri = parse_qs(urlsplit(authorization_url).query)["redirect_uri"][0]
    async with httpx.AsyncClient(
        transport=_MappedTransport(), follow_redirects=False, timeout=15.0
    ) as dex:

        async def follow(response: httpx.Response) -> httpx.Response:
            for _ in range(10):
                if response.status_code not in (302, 303):
                    return response
                location = urljoin(str(response.url), response.headers["location"])
                if location.startswith(redirect_uri):
                    return response
                response = await dex.get(location)
            raise AssertionError("Dex redirected more than ten times")

        response = await follow(await dex.get(authorization_url))
        if response.status_code == 400 and "Unregistered redirect_uri" in response.text:
            pytest.fail(
                f"Dex refuses the stack's redirect URI {redirect_uri} as unregistered: the "
                "running lab was started for another HERD_GATEWAY_BIND (`make oidc-down`, "
                "then rerun the phase)."
            )
        if response.status_code == 200:
            form = re.search(r'<form[^>]*action="([^"]*)"', response.text)
            assert form is not None, f"no login form at {response.url.path}"
            post_url = urljoin(str(response.url), unescape(form.group(1)))
            response = await follow(
                await dex.post(post_url, data={"login": email, "password": PASSWORD})
            )
        assert response.status_code in (302, 303), (
            f"Dex answered {response.status_code} at {response.url.path}, not a redirect"
        )
        location = response.headers["location"]
        assert location.startswith(redirect_uri + "?"), location
        return location


def _set_cookie_values(response: httpx.Response, name: str) -> list[str]:
    """The values `response` sets for cookie `name` (an empty string clears it)."""
    values = []
    for header in response.headers.get_list("set-cookie"):
        cookie_name, _, rest = header.partition("=")
        if cookie_name.strip() == name:
            values.append(rest.split(";", 1)[0].strip().strip('"'))
    return values


async def begin_and_sign_in(client: httpx.AsyncClient, email: str) -> httpx.Response:
    """`GET /oidc/login`, Dex's form as `email`, then the callback; the callback's answer."""
    begin = await client.get(f"{BASE_URL}/auth/oidc/login")
    assert begin.status_code == 302, begin.text
    authorization_url = begin.headers["location"]
    assert authorization_url.startswith(f"{ISSUER}/auth?"), authorization_url
    flow_values = _set_cookie_values(begin, FLOW_COOKIE)
    assert len(flow_values) == 1 and flow_values[0], "the begin route set no flow cookie"
    callback_url = await dex_sign_in(authorization_url, email)
    gateway = urlsplit(BASE_URL)
    callback = urlsplit(callback_url)
    assert (callback.scheme, callback.netloc) == (gateway.scheme, gateway.netloc), (
        f"the stack's OIDC_REDIRECT_URI ({callback_url.split('?', 1)[0]}) is not on the "
        f"gateway address the tests use ({gateway.scheme}://{gateway.netloc})"
    )
    return await client.get(callback_url)


async def sign_in_tokens(client: httpx.AsyncClient, email: str) -> dict:
    """A full sign-in as `email` ending in the exchange; the exchange's 200 body."""
    callback = await begin_and_sign_in(client, email)
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == SUCCESS_LOCATION
    exchange = await client.post(f"{BASE_URL}/auth/oidc/exchange", json={})
    assert exchange.status_code == 200, exchange.text
    return exchange.json()


def _browser() -> httpx.AsyncClient:
    """A fresh cookie jar, as one browser."""
    return httpx.AsyncClient(verify=False, timeout=30.0, follow_redirects=False)


async def _me(token: str) -> httpx.Response:
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        return await client.get(f"{BASE_URL}/auth/me", headers={"Authorization": f"Bearer {token}"})


def _account_row(email: str) -> list[str]:
    """`id|username|auth_source|role|is_active` of the account with `email`, or []."""
    row = _sql(
        "SELECT id || '|' || username || '|' || auth_source || '|' || role || '|' || is_active "
        f"FROM auth.users WHERE email = {_quote(email)}"
    )
    return row.split("|") if row else []


def _identity_count(subject: str) -> int:
    return int(
        _sql(
            "SELECT count(*) FROM auth.federated_identities "
            f"WHERE issuer = {_quote(ISSUER)} AND subject = {_quote(subject)}"
        )
    )


# --- the tests ---


async def test_login_options_offer_single_sign_on_and_no_password_form():
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.get(f"{BASE_URL}/auth/login-options")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "auth_method": "oidc",
        "registration_open": False,
        "password_login": False,
        "sso": {"provider_name": PROVIDER_NAME},
    }


async def test_first_sign_in_provisions_an_oidc_account_and_the_second_reuses_it():
    assert _account_row(ALICE) == []
    async with _browser() as client:
        body = await sign_in_tokens(client, ALICE)
        assert body["token_type"] == "bearer"
        assert body["next"] == "/topology"
        assert body["access_token"] and body["refresh_token"]
        # The handoff is single-use, and the 200 cleared its cookie.
        again = await client.post(f"{BASE_URL}/auth/oidc/exchange", json={})
        assert again.status_code == 401
        assert again.json() == {"detail": EXCHANGE_REFUSED_DETAIL}

    me = await _me(body["access_token"])
    assert me.status_code == 200, me.text
    profile = me.json()
    assert (profile["email"], profile["username"], profile["full_name"], profile["role"]) == (
        ALICE,
        "alice.sso",
        "Alice Example",
        "user",
    )
    user_id, username, auth_source, role, is_active = _account_row(ALICE)
    assert (user_id, username, auth_source, role, is_active) == (
        profile["id"],
        "alice.sso",
        "oidc",
        "USER",
        "true",
    )
    subject = dex_subject(ALICE_USER_ID)
    assert (
        _sql(
            "SELECT user_id FROM auth.federated_identities "
            f"WHERE issuer = {_quote(ISSUER)} AND subject = {_quote(subject)}"
        )
        == profile["id"]
    )

    # The refresh token works like a password login's.
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        refreshed = await client.post(
            f"{BASE_URL}/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
    assert refreshed.status_code == 200, refreshed.text

    async with _browser() as client:
        second = await sign_in_tokens(client, ALICE)
    me_again = await _me(second["access_token"])
    assert me_again.json()["id"] == profile["id"]
    assert _sql(f"SELECT count(*) FROM auth.users WHERE email = {_quote(ALICE)}") == "1"
    assert _identity_count(subject) == 1


async def test_the_minted_token_authorizes_a_call_on_another_service():
    async with _browser() as client:
        body = await sign_in_tokens(client, CAROL)
    token = body["access_token"]
    me = await _me(token)
    assert me.json()["username"] == "carol.sso"
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        anonymous = await client.get(f"{BASE_URL}/reservations/")
        signed_in = await client.get(
            f"{BASE_URL}/reservations/", headers={"Authorization": f"Bearer {token}"}
        )
    assert anonymous.status_code == 401
    assert signed_in.status_code == 200, signed_in.text


async def test_register_is_refused_with_the_single_sign_on_detail():
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.post(
            f"{BASE_URL}/auth/register",
            json={
                "email": "oidc-it-register@herd.example",
                "username": "oidc-it-register",
                "password": "Password123!",
            },
        )
    assert resp.status_code == 409
    assert resp.json() == {"detail": REGISTER_DISABLED_DETAIL}
    assert _account_row("oidc-it-register@herd.example") == []


async def test_the_seeded_superadmin_password_login_is_refused_with_break_glass_off():
    # The account exists, is an active local superadmin, and SEED_PASSWORD is
    # its password, so the 401 below is the mode's refusal and nothing else.
    _, _, auth_source, role, is_active = _account_row(SEED_EMAIL)
    assert (auth_source, role, is_active) == ("local", "SUPERADMIN", "true")
    stored = _sql(f"SELECT hashed_password FROM auth.users WHERE email = {_quote(SEED_EMAIL)}")
    # Auth hashes the password's first 72 bytes (services/auth/app/services/passwords.py).
    assert bcrypt.checkpw(SEED_PASSWORD.encode()[:72], stored.encode()), (
        "SEED_PASSWORD is not the seeded superadmin's password; the refusal would prove nothing"
    )
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.post(
            f"{BASE_URL}/auth/login", json={"email": SEED_EMAIL, "password": SEED_PASSWORD}
        )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid credentials"}
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_a_provider_user_with_a_local_accounts_email_is_refused_and_never_linked(
    local_collider,
):
    before = _account_row(COLLIDER)
    async with _browser() as client:
        callback = await begin_and_sign_in(client, COLLIDER)
    assert callback.status_code == 302
    assert callback.headers["location"] == FAILED_LOCATION
    assert _set_cookie_values(callback, HANDOFF_COOKIE) == []
    assert _set_cookie_values(callback, FLOW_COOKIE) == [""]
    assert _account_row(COLLIDER) == before
    assert before[0] == local_collider and before[2] == "local"
    assert _identity_count(dex_subject(COLLIDER_USER_ID)) == 0
    assert _sql("SELECT count(*) FROM auth.users WHERE username = 'collide.sso'") == "0"


async def test_a_provider_user_whose_email_is_unverified_is_refused():
    async with _browser() as client:
        callback = await begin_and_sign_in(client, UNVERIFIED)
    assert callback.status_code == 302
    assert callback.headers["location"] == FAILED_LOCATION
    assert _set_cookie_values(callback, HANDOFF_COOKIE) == []
    assert _account_row(UNVERIFIED) == []
    assert _identity_count(dex_subject(UNVERIFIED_USER_ID)) == 0
