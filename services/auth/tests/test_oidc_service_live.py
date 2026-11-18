"""Live OIDC tests against the checked-in Dex provider (ADR 0016 phase 5a, D12).

Functional level: HERD's real OIDC code (`oidc_service`, and the routes in
`app/routers/oidc.py` through the auth app over ASGI, in-memory SQLite as in
`test_oidc_routes.py`) against a real identity provider, the Dex in
infra/oidc-test. Each sign-in is driven over HTTP through Dex's own login form,
with no browser. The locally signed tokens of the unit and routes suites cannot
catch a provider that behaves differently from the fake; these tests can. As a
whole the suite fails if HERD stops sending PKCE, stops sending or checking the
nonce, reads the wrong claim or the wrong subject, finds an identity by issuer
alone, refuses Dex's signing keys, ignores `email_verified`, skips the email
collision check, or logs Dex's error text; each test fails under at least one
of those mutations, and no test catches all of them (the mutation table in
docs/specs/TESTING.md says which test catches which).

Skipped automatically when nothing listens on HERD_TEST_OIDC_HOST and
HERD_TEST_OIDC_PORT (default 127.0.0.1:5556), so `make test-auth` stays green
without the provider. To run: `make oidc-up test-auth-oidc`. Setting
HERD_TEST_OIDC_REQUIRED=1 (as `make test-auth-oidc` and the gates do) turns the
skip into a failure of every test that names the remedy. When the port answers
but the discovery document there is not this lab's (another server on the
port, or a Dex started from an older dex.yaml), every test fails with the cause
and the remedy, required or not, as the live LDAP suite does for a stale seed.

Issuer reachability: the issuer is `http://oidc-test:5556/dex` for every
client. This host-side suite cannot resolve `oidc-test`, so `oidc_service`'s
`transport` seam gets `_MappedTransport`. Before each request goes out it
rewrites the request URL's host and port from `oidc-test:5556` to the published
address; the Host header, fixed when the request was built, still names the
issuer. Every URL HERD builds or checks (the discovery URL, the endpoints in the
discovery document, the issuer in the token) is the issuer's before the
transport sees it, and HERD reads only a response's status and body, never its
URL, so the rewrite cannot change a decision HERD makes. Two side effects: the
rewritten URL is what `response.request.url` and httpx's request log line show,
and a client given a transport no longer applies its own `verify=` setting,
which is moot here because the lab is plain HTTP.

Fixtures, from infra/oidc-test/dex.yaml (test-only values):

- alice@oidc.example.com: `preferred_username` alice.sso, `name` Alice Example;
- user1@herd.dev: the email of a seeded local account (collision);
- bob@oidc.example.com: `email_verified` false;
- carol@oidc.example.com: a second normal user (`preferred_username` carol.sso).

Every user's password is `oidc-test-password`; the client is `herd` with the
secret `herd-oidc-test-client-secret`.
"""

from __future__ import annotations

import base64
import json
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
from app.models.user import Role, User
from app.routers import oidc as oidc_router
from app.services import oidc_service
from app.services.auth_service import create_user
from app.services.oidc_service import OidcRejectedError
from app.utils.jwt import create_access_token
from herd_common.logging import JSONFormatter
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
CAROL = "carol@oidc.example.com"
ALICE_USER_ID = "6f1c5a0e-1b7e-4d0a-9a51-0c6b1f0e0001"
CAROL_USER_ID = "6f1c5a0e-1b7e-4d0a-9a51-0c6b1f0e0004"
# alice's `sub` as Dex v2.46.1 sent it in the phase 5a measurement (2026-11-14);
# dex_subject() derives it from ALICE_USER_ID, and the full sign-in test checks both.
ALICE_SUBJECT = "CiQ2ZjFjNWEwZS0xYjdlLTRkMGEtOWE1MS0wYzZiMWYwZTAwMDESBWxvY2Fs"

# What Dex answers a wrong code_verifier with (phase 5a measurement): none of it
# may reach an emitted log line.
DEX_PKCE_ERROR_DESCRIPTION = "Invalid code_verifier."
DEX_PKCE_ERROR_BODY = '{"error":"invalid_grant","error_description":"Invalid code_verifier."}'

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


def _foreign_provider_problem() -> str | None:
    """Why whatever answers on the port is not infra/oidc-test's Dex, or None.

    The issuer is the lab's own, and the PKCE methods are dex.yaml's (S256
    only), which a Dex started from another configuration would not advertise."""
    url = f"http://{OIDC_HOST}:{OIDC_PORT}/dex/.well-known/openid-configuration"
    try:
        response = httpx.get(url, headers={"Host": f"{ISSUER_HOST}:{ISSUER_PORT}"}, timeout=5.0)
    except httpx.HTTPError as exc:
        return f"the discovery request failed ({type(exc).__name__})"
    if response.status_code != 200:
        return f"the discovery document answered HTTP {response.status_code}"
    try:
        document = response.json()
    except ValueError:
        return "the discovery document is not JSON"
    if not isinstance(document, dict) or document.get("issuer") != ISSUER:
        issuer = document.get("issuer") if isinstance(document, dict) else None
        return f"the discovery document names the issuer {issuer!r}, not {ISSUER!r}"
    if document.get("code_challenge_methods_supported") != ["S256"]:
        return "the provider's PKCE methods are not infra/oidc-test/dex.yaml's (an older dex.yaml?)"
    return None


@pytest.fixture(scope="session", autouse=True)
def _fail_on_a_foreign_provider():
    if _OIDC_REACHABLE:
        problem = _foreign_provider_problem()
        if problem is not None:
            pytest.fail(
                f"Something answers at {OIDC_HOST}:{OIDC_PORT} but it is not the "
                f"infra/oidc-test provider: {problem}. Run `make oidc-down oidc-up`, or "
                f"stop whatever listens on port {OIDC_PORT} (`ss -ltnp`)."
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
    """Sends a request for `oidc-test:5556` to the published address instead.

    It rewrites `request.url` itself (httpx has no public hook for the connect
    target), so the response's request URL and httpx's log line show the
    published address; the Host header, set when the request was built, still
    names the issuer, as with curl's --resolve. The module docstring says why
    this cannot change a decision HERD makes."""

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
    (it names HERD's callback, which the caller drives itself). The first request
    goes through `_MappedTransport`; later ones are joined to the rewritten
    response URL, so they address the published address directly, which Dex
    accepts since it does not check the Host header."""
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


def dex_subject(user_id: str, connector_id: str = "local") -> str:
    """The `sub` Dex issues for a static user: the unpadded base64url encoding of a
    protobuf message with the userID as field 1 and the connector id as field 2,
    each a length-delimited string (tags 0x0A and 0x12)."""
    message = b"".join(
        bytes([tag, len(value)]) + value
        for tag, value in ((0x0A, user_id.encode()), (0x12, connector_id.encode()))
    )
    return base64.urlsafe_b64encode(message).rstrip(b"=").decode("ascii")


async def signed_in_profile(client, email: str) -> dict:
    """A full sign-in as `email` through the routes; returns `GET /me`'s body."""
    callback = await route_sign_in(client, email)
    assert callback.headers["location"] == SUCCESS
    exchanged = await exchange(client, _cookie(callback, oidc_router.HANDOFF_COOKIE))
    assert exchanged.status_code == 200, exchanged.text
    me = await client.get(
        "/me", headers={"Authorization": f"Bearer {exchanged.json()['access_token']}"}
    )
    assert me.status_code == 200, me.text
    return me.json()


def _string_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _string_values(item)]
    if isinstance(value, list):
        return [s for item in value for s in _string_values(item)]
    return []


def emitted_text(records: list[logging.LogRecord]) -> list[str]:
    """Each record as auth emits it, through the real JSONFormatter (extras
    included), plus every string value of the decoded line, so text that JSON
    escapes (a quoted body) is matched as it was logged."""
    formatter = JSONFormatter("auth")
    texts: list[str] = []
    for record in records:
        line = formatter.format(record)
        texts.append(line)
        texts.extend(_string_values(json.loads(line)))
    return texts


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
    assert dex_subject(ALICE_USER_ID) == ALICE_SUBJECT
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
    assert identity.subject == ALICE_SUBJECT
    assert identity.user_id == user.id

    again = await signed_in_profile(client, ALICE)
    assert again["id"] == profile["id"]
    assert await row_counts() == (1, 1)


async def test_a_second_dex_user_gets_a_separate_account(live_oidc, client):
    """The identity is the issuer AND the subject: carol, from the same issuer,
    signs in after alice and gets her own account and her own identity row."""
    alice = await signed_in_profile(client, ALICE)
    carol = await signed_in_profile(client, CAROL)

    assert carol["id"] != alice["id"]
    assert (carol["email"], carol["username"]) == (CAROL, "carol.sso")
    async with TestSessionLocal() as db:
        identities = (await db.execute(select(FederatedIdentity))).scalars().all()
    assert {(str(i.user_id), i.issuer, i.subject) for i in identities} == {
        (alice["id"], ISSUER, dex_subject(ALICE_USER_ID)),
        (carol["id"], ISSUER, dex_subject(CAROL_USER_ID)),
    }
    assert await row_counts() == (2, 2)


async def test_a_wrong_code_verifier_is_refused_by_dex(live_oidc, caplog):
    """Dex enforces PKCE: the code HERD's own authorization request obtained is
    refused at the token endpoint with another verifier, which HERD maps to
    `token_refused` with none of Dex's error text."""
    caplog.set_level(logging.DEBUG)
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
    # Nothing Dex sent, neither its error_description nor its body, reaches an
    # emitted log line: in a message or in any extra field.
    emitted = emitted_text(caplog.get_records("call"))
    assert emitted, "no log record reached the leak check"
    leaks = [
        text
        for text in emitted
        for needle in (DEX_PKCE_ERROR_DESCRIPTION, DEX_PKCE_ERROR_BODY)
        if needle in text
    ]
    assert leaks == []


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


# --- recovery through the superadmin routes (issue #1399) ---
#
# The lab's Dex serves one issuer, and a static user's subject is fixed by its
# userID, so neither an issuer change nor a reissued subject can happen at Dex
# itself. Each test instead puts HERD's stored identity where an issuer change
# or a reissued subject leaves it (through the recovery routes themselves, so
# no row is written by hand), then shows Dex's real sign-in refused, runs the
# recovery, and shows Dex's next real sign-in landing in the same account.

PREVIOUS_ISSUER = "http://oidc-test:5556/dex-previous"


async def _superadmin_headers() -> dict[str, str]:
    async with TestSessionLocal() as db:
        sa = await create_user(
            db, "root@herd.example", "root", "root-password-1", role=Role.SUPERADMIN
        )
    token = create_access_token(
        {"sub": str(sa.id), "username": sa.username, "email": sa.email, "role": "superadmin"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _rewrite(client, headers, old: str, new: str, dry_run: bool = False) -> dict:
    resp = await client.post(
        "/admin/federated-identities/rewrite-issuer",
        json={"old_issuer": old, "new_issuer": new, "dry_run": dry_run},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _relink(client, headers, user_id: str, issuer: str, subject: str) -> None:
    resp = await client.put(
        f"/users/{user_id}/federated-identity",
        json={"issuer": issuer, "subject": subject},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_an_issuer_change_is_refused_by_dex_sign_in_then_recovered_by_the_rewrite(
    live_oidc, client, caplog
):
    caplog.set_level(logging.INFO)
    headers = await _superadmin_headers()
    alice = await signed_in_profile(client, ALICE)
    # HERD's state after OIDC_ISSUER moved from PREVIOUS_ISSUER to Dex's issuer:
    # alice's identity still names the previous one.
    assert (await _rewrite(client, headers, ISSUER, PREVIOUS_ISSUER))["rewritten"] == 1

    callback = await route_sign_in(client, ALICE)
    assert callback.headers["location"] == FAILED
    assert refusal_reasons(caplog) == ["email_taken"]
    assert await row_counts() == (2, 1)

    dry = await _rewrite(client, headers, PREVIOUS_ISSUER, ISSUER, dry_run=True)
    assert dry == {"dry_run": True, "matched": 1, "conflicts": 0, "rewritten": 0}
    assert (await _rewrite(client, headers, PREVIOUS_ISSUER, ISSUER))["rewritten"] == 1

    again = await signed_in_profile(client, ALICE)
    assert again["id"] == alice["id"]
    assert await row_counts() == (2, 1)


async def test_a_reissued_subject_is_refused_by_dex_sign_in_then_recovered_by_the_relink(
    live_oidc, client, caplog
):
    caplog.set_level(logging.INFO)
    headers = await _superadmin_headers()
    alice = await signed_in_profile(client, ALICE)
    # HERD's state when alice's account was linked to a subject Dex no longer
    # issues her (an account deleted and recreated at the provider).
    await _relink(client, headers, alice["id"], ISSUER, "subject-before-the-reissue")

    callback = await route_sign_in(client, ALICE)
    assert callback.headers["location"] == FAILED
    assert refusal_reasons(caplog) == ["email_taken"]

    # The superadmin reads alice's subject at the provider (for this lab, the
    # encoding of her userID in dex.yaml) and names it.
    await _relink(client, headers, alice["id"], ISSUER, dex_subject(ALICE_USER_ID))
    again = await signed_in_profile(client, ALICE)
    assert again["id"] == alice["id"]
    async with TestSessionLocal() as db:
        identity = (await db.execute(select(FederatedIdentity))).scalar_one()
    assert (str(identity.user_id), identity.issuer, identity.subject) == (
        alice["id"],
        ISSUER,
        ALICE_SUBJECT,
    )
