"""Live OIDC tests against the checked-in Dex provider (ADR 0016 phase 5a, D12).

Functional level: HERD's real OIDC code (`oidc_service`, and the routes in
`app/routers/oidc.py` through the auth app over ASGI, in-memory SQLite as in
`test_oidc_routes.py`) against a real identity provider, the Dex in
infra/oidc-test. Each sign-in is driven over HTTP through Dex's own login form,
with no browser. The locally signed tokens of the unit and routes suites cannot
catch a provider that behaves differently from the fake; these tests can, and
each one fails if HERD stops sending PKCE, stops checking the nonce, or reads
the wrong claim (the mutation proofs are recorded in docs/specs/TESTING.md).

Skipped automatically when nothing listens on HERD_TEST_OIDC_HOST and
HERD_TEST_OIDC_PORT (default 127.0.0.1:5556), so `make test-auth` stays green
without the provider. To run: `make oidc-up test-auth-oidc`. Setting
HERD_TEST_OIDC_REQUIRED=1 (as `make test-auth-oidc` and the gates do) turns the
skip into a failure of every test that names the remedy.

Issuer reachability: the issuer is `http://oidc-test:5556/dex` for every
client. This host-side suite cannot resolve `oidc-test`, so `oidc_service`'s
`transport` seam gets `_MappedTransport`, which connects to the published
address while every URL and the Host header stay the issuer's. It changes where
the TCP connection goes and nothing else: discovery, the authorization URL,
the token request, and every check are HERD's own code.

Fixtures, from infra/oidc-test/dex.yaml (test-only values):

- alice@oidc.example.com: `preferred_username` alice.sso, `name` Alice Example;
- user1@herd.dev: the email of a seeded local account (collision);
- bob@oidc.example.com: `email_verified` false.

Every user's password is `oidc-test-password`; the client is `herd` with the
secret `herd-oidc-test-client-secret`.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import pytest
from app.config import settings
from app.models.federated_identity import FederatedIdentity
from app.models.user import User
from app.routers import oidc as oidc_router
from app.services import oidc_service
from app.services.auth_service import create_user
from app.services.oidc_service import OidcRejectedError
from sqlalchemy import func, select

from tests._harness import TestSessionLocal

OIDC_HOST = os.getenv("HERD_TEST_OIDC_HOST", "127.0.0.1")
OIDC_PORT = int(os.getenv("HERD_TEST_OIDC_PORT", "5556"))
ISSUER_HOST = "oidc-test"
ISSUER_PORT = 5556
ISSUER = f"http://{ISSUER_HOST}:{ISSUER_PORT}/dex"
CLIENT_ID = "herd"
CLIENT_SECRET = "herd-oidc-test-client-secret"
REDIRECT_URI = "https://localhost/api/auth/oidc/callback"
PASSWORD = "oidc-test-password"

ALICE = "alice@oidc.example.com"
COLLIDER = "user1@herd.dev"
UNVERIFIED = "bob@oidc.example.com"

SUCCESS = "/login/sso"
FAILED = "/login?sso_error=failed"


def _oidc_reachable() -> bool:
    try:
        with socket.create_connection((OIDC_HOST, OIDC_PORT), timeout=1.0):
            return True
    except OSError:
        return False


_OIDC_REQUIRED = os.getenv("HERD_TEST_OIDC_REQUIRED", "") not in ("", "0")
_OIDC_REACHABLE = _oidc_reachable()

pytestmark = pytest.mark.skipif(
    not _OIDC_REQUIRED and not _OIDC_REACHABLE,
    reason=(
        f"No OIDC provider reachable at {OIDC_HOST}:{OIDC_PORT}; "
        "start the test provider to run (make oidc-up)."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _OIDC_REQUIRED and not _OIDC_REACHABLE:
        pytest.fail(
            f"HERD_TEST_OIDC_REQUIRED is set but no OIDC provider is reachable at "
            f"{OIDC_HOST}:{OIDC_PORT}; run `make oidc-up` (infra/oidc-test) or unset "
            "HERD_TEST_OIDC_REQUIRED."
        )


class _MappedTransport(httpx.AsyncHTTPTransport):
    """Connects `oidc-test:5556` to the published address; the request URL's host
    is replaced only for the connection, so the Host header, set when the request
    was built, still names the issuer (curl's --resolve, Chromium's
    --host-resolver-rules)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == ISSUER_HOST and request.url.port == ISSUER_PORT:
            request.url = request.url.copy_with(host=OIDC_HOST, port=OIDC_PORT)
        return await super().handle_async_request(request)


@pytest.fixture
def live_oidc(monkeypatch):
    """OIDC mode with the lab's settings, the mapped transport, empty caches."""
    for name, value in {
        "auth_method": "oidc",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_redirect_uri": REDIRECT_URI,
        "oidc_scopes": "openid email profile",
        "oidc_id_token_algorithm": "RS256",
        "oidc_username_claim": "preferred_username",
        "oidc_email_claim": "email",
        # The lab is plain HTTP, the LDAP_USE_TLS=false precedent (ADR 0016 D12).
        "oidc_require_https": False,
        "oidc_ca_cert": "",
        "oidc_provider_name": "Dex",
        "oidc_break_glass_local_superadmin": False,
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(oidc_service, "transport", _MappedTransport())
    oidc_service.reset_caches()
    yield
    oidc_service.reset_caches()


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


# --- driving Dex over HTTP, as a browser would ---


async def dex_sign_in(authorization_url: str, email: str) -> dict[str, str]:
    """Follow `authorization_url` through Dex's login form as `email` and return
    the query Dex redirects to REDIRECT_URI with (`code` and `state`, or `error`).

    Redirects inside Dex are followed by hand; the one to REDIRECT_URI is not
    (it names HERD's callback, which the caller drives itself)."""
    async with httpx.AsyncClient(transport=_MappedTransport(), follow_redirects=False) as dex:

        async def follow(response: httpx.Response) -> httpx.Response:
            for _ in range(10):
                if response.status_code not in (302, 303):
                    return response
                location = urljoin(str(response.url), response.headers["location"])
                if location.startswith(REDIRECT_URI):
                    return response
                response = await dex.get(location)
            raise AssertionError("Dex redirected more than ten times")

        response = await follow(await dex.get(authorization_url))
        if response.status_code == 200:
            form = re.search(r'<form[^>]*action="([^"]*)"', response.text)
            assert form is not None, f"no login form at {response.url.path}"
            post_url = urljoin(str(response.url), unescape(form.group(1)))
            response = await follow(
                await dex.post(post_url, data={"login": email, "password": PASSWORD})
            )
        assert response.status_code in (302, 303), (
            f"Dex answered {response.status_code} at {response.url.path}, not a redirect "
            "to the callback"
        )
        location = response.headers["location"]
        assert location.startswith(REDIRECT_URI + "?"), location
        return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _cookie(response: httpx.Response, name: str) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        first = header.split(";", 1)[0]
        cookie_name, _, value = first.partition("=")
        if cookie_name.strip() == name:
            return value
    return None


async def route_sign_in(client, email: str) -> httpx.Response:
    """GET /oidc/login, Dex's login form as `email`, then GET /oidc/callback with
    the binding cookie; returns the callback's response."""
    begun = await client.get("/oidc/login")
    assert begun.status_code == 302, begun.text
    authorization_url = begun.headers["location"]
    assert authorization_url.startswith(ISSUER + "/auth?"), "begin did not redirect to Dex"
    binding = _cookie(begun, oidc_router.FLOW_COOKIE)
    assert binding, "begin set no flow cookie"

    params = await dex_sign_in(authorization_url, email)
    assert "error" not in params, f"Dex refused the authorization request: {params['error']}"
    sent_state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    assert params["state"] == sent_state

    callback = await client.get(
        "/oidc/callback?" + urlencode(params),
        headers={"Cookie": f"{oidc_router.FLOW_COOKIE}={binding}"},
    )
    assert callback.status_code == 302, callback.text
    return callback


async def exchange(client, handoff: str) -> httpx.Response:
    return await client.post(
        "/oidc/exchange",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Cookie": f"{oidc_router.HANDOFF_COOKIE}={handoff}",
        },
    )


async def row_counts() -> tuple[int, int]:
    async with TestSessionLocal() as db:
        users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        identities = (
            await db.execute(select(func.count()).select_from(FederatedIdentity))
        ).scalar_one()
    return users, identities


def refusal_reasons(caplog) -> list[str]:
    return [
        r.reason
        for r in caplog.records
        if getattr(r, "action", None) == "login_failure" and getattr(r, "source", None) == "oidc"
    ]


# --- the tests ---


async def test_discovery_from_dex_passes_herd_validation(live_oidc):
    """HERD fetches and validates Dex's discovery document and builds its
    authorization URL on Dex's endpoint with PKCE S256."""
    request = await oidc_service.build_authorization_request()
    parts = urlsplit(request.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ISSUER + "/auth"
    query = parse_qs(parts.query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] == [request.nonce]


async def test_jwks_from_dex_yields_a_signing_key_for_every_rs256_key(live_oidc):
    """HERD's key selection (`_signing_key`, the selection it runs for every ID
    token) accepts each RS256 signing key Dex publishes."""
    async with httpx.AsyncClient(transport=_MappedTransport()) as dex:
        keys = (await dex.get(ISSUER + "/keys")).json()["keys"]
    rs256 = [k for k in keys if k.get("kty") == "RSA" and k.get("use", "sig") == "sig"]
    assert rs256, "Dex published no RSA signing key"
    for jwk in rs256:
        key = await oidc_service._signing_key(jwk["kid"], "RS256")
        assert key.key_id == jwk["kid"]


async def test_full_sign_in_through_the_routes_provisions_then_reuses_the_account(
    live_oidc, client
):
    """GET /oidc/login, Dex's login form, GET /oidc/callback, POST /oidc/exchange,
    then GET /me: the account is provisioned from Dex's claims (username from
    `preferred_username`, full name from `name`), and a second sign-in reuses it."""
    callback = await route_sign_in(client, ALICE)
    assert callback.headers["location"] == SUCCESS
    handoff = _cookie(callback, oidc_router.HANDOFF_COOKIE)
    assert handoff

    exchanged = await exchange(client, handoff)
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    assert body["next"] == "/topology"

    me = await client.get("/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200, me.text
    profile = me.json()
    assert profile["email"] == ALICE
    assert profile["username"] == "alice.sso"
    assert profile["full_name"] == "Alice Example"
    assert profile["role"] == "user"

    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == ALICE))).scalar_one()
        identity = (await db.execute(select(FederatedIdentity))).scalar_one()
    assert user.auth_source == "oidc"
    assert user.hashed_password is None
    assert identity.issuer == ISSUER
    assert identity.user_id == user.id

    again = await route_sign_in(client, ALICE)
    assert again.headers["location"] == SUCCESS
    second = await exchange(client, _cookie(again, oidc_router.HANDOFF_COOKIE))
    assert second.status_code == 200, second.text
    me_again = await client.get(
        "/me", headers={"Authorization": f"Bearer {second.json()['access_token']}"}
    )
    assert me_again.json()["id"] == profile["id"]
    assert await row_counts() == (1, 1)


async def test_a_wrong_code_verifier_is_refused_by_dex(live_oidc, caplog):
    """Dex enforces PKCE: the code HERD's own authorization request obtained is
    refused at the token endpoint with another verifier, which HERD maps to
    `token_refused` with none of Dex's error text."""
    caplog.set_level(logging.INFO)
    request = await oidc_service.build_authorization_request()
    params = await dex_sign_in(request.url, ALICE)
    assert "error" not in params, f"Dex refused the authorization request: {params['error']}"

    wrong = "W" * oidc_service.CODE_VERIFIER_LENGTH
    with pytest.raises(OidcRejectedError) as refused:
        await oidc_service.redeem_code(params["code"], wrong, request.nonce)
    assert refused.value.reason == "token_refused"
    assert str(refused.value) == "single sign-on refused (token_refused)"

    records = [r for r in caplog.records if getattr(r, "action", None) == "oidc_rejected"]
    assert [(r.reason, r.status_code, r.oauth_error) for r in records] == [
        ("token_refused", 400, "invalid_grant")
    ]
    # Dex's error_description ("Invalid code_verifier.") reaches no log record.
    assert not [r for r in caplog.records if "code_verifier." in r.getMessage()]


async def test_a_nonce_other_than_the_flow_nonce_is_refused_by_herd(live_oidc):
    """Dex echoes the nonce HERD sent in the ID token; redeeming that token
    against any other nonce is refused as `nonce_mismatch`."""
    request = await oidc_service.build_authorization_request()
    params = await dex_sign_in(request.url, ALICE)
    assert "error" not in params, f"Dex refused the authorization request: {params['error']}"

    other_nonce = secrets.token_urlsafe(oidc_service.NONCE_BYTES)
    with pytest.raises(OidcRejectedError) as refused:
        await oidc_service.redeem_code(params["code"], request.code_verifier, other_nonce)
    assert refused.value.reason == "nonce_mismatch"
    assert str(refused.value) == "single sign-on refused (nonce_mismatch)"


async def test_a_dex_user_with_a_local_accounts_email_is_refused_and_never_linked(
    live_oidc, client, caplog
):
    """ADR 0016 D4: Dex's user1@herd.dev equals a local account's email, so the
    first sign-in is refused as `email_taken` and the local account is untouched."""
    caplog.set_level(logging.INFO)
    async with TestSessionLocal() as db:
        local = await create_user(db, COLLIDER, "user1", "user1user1xx")
    callback = await route_sign_in(client, COLLIDER)

    assert callback.headers["location"] == FAILED
    assert _cookie(callback, oidc_router.HANDOFF_COOKIE) is None
    assert refusal_reasons(caplog) == ["email_taken"]
    assert await row_counts() == (1, 0)
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == COLLIDER))).scalar_one()
    assert (user.id, user.auth_source, user.username) == (local.id, "local", "user1")


async def test_a_dex_user_whose_email_is_unverified_is_refused(live_oidc, client, caplog):
    """ADR 0016 D4: Dex sends `email_verified: false` for bob, so his first
    sign-in is refused as `email_unverified` and nothing is provisioned."""
    caplog.set_level(logging.INFO)
    callback = await route_sign_in(client, UNVERIFIED)

    assert callback.headers["location"] == FAILED
    assert refusal_reasons(caplog) == ["email_unverified"]
    assert await row_counts() == (0, 0)
