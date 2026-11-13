"""The single sign-on routes through the real auth app (ADR 0016 phase 3, issue #37).

Functional level: `GET /oidc/login`, `GET /oidc/callback`, and `POST /oidc/exchange`
driven over HTTP (ASGI transport, in-memory SQLite), then `GET /me` and `POST /refresh`
with the tokens the exchange answers. The identity provider is a fake behind
`httpx.MockTransport` on `oidc_service.transport`, signing ID tokens with a test RSA key
generated per module; no key material is committed.

What is pinned:

- every route answers 404 outside OIDC mode before it reads any input or opens a
  database session (`test_every_route_answers_404_outside_oidc_mode_before_reading_input`);
- the begin route's redirect, cookie, and flow row, every way it answers
  `unavailable`, and the raw `next` reaching the flow store;
- every row of ADR 0016's callback outcome table, each failure after the claim moving
  the row to `failed`, and every callback clearing `herd_oidc_flow`;
- every D13 refusal that can be driven over HTTP (code injection, PKCE, login CSRF,
  state and handoff replay, nonce, token substitution, issuer checks, algorithm
  confusion, unknown keys, expiry, open redirect, host headers, takeover by email,
  pruning, deactivated users), and the `at_hash` non-check;
- every exchange failure and each 422 shape, and both cookies' attributes;
- the catch-alls: an unexpected exception in the begin or the callback answers
  `unavailable` and is logged by class name only;
- log hygiene: the `idp` fixture formats every log record of every test through
  `JSONFormatter` and fails the test when any secret or provider text appears (client
  secret, code, state, nonce, verifier, binding and handoff secrets, the authorization
  URL, ID tokens, the provider's and HERD's tokens, and the claim values).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from app.config import settings
from app.database import get_db
from app.main import app
from app.models.federated_identity import FederatedIdentity
from app.models.oidc_login_flow import OidcLoginFlow
from app.models.user import RefreshToken, User
from app.routers import oidc as oidc_router
from app.services import auth_service, oidc_flow_service, oidc_service
from app.services.auth_service import create_user, set_user_active
from app.services.oidc_service import VerifiedIdToken
from app.utils.jwt import hash_token
from cryptography.hazmat.primitives.asymmetric import rsa
from herd_common.logging import JSONFormatter
from sqlalchemy import func, select

from tests._harness import TestSessionLocal

ISSUER = "https://idp.test/realms/herd"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = ISSUER + "/protocol/openid-connect/auth"
TOKEN_ENDPOINT = ISSUER + "/protocol/openid-connect/token"
JWKS_URI = ISSUER + "/protocol/openid-connect/certs"
CLIENT_ID = "herd-client"
# At least 32 bytes, so an HS256 token keyed by it raises no short-key warning.
CLIENT_SECRET = "SENTINEL-client-secret-0123456789abcdef"
REDIRECT_URI = "https://herd.test/api/auth/oidc/callback"
COOKIE_PATH = "/api/auth/oidc"
CODE = "SENTINEL-authorization-code"
KID = "SENTINEL-kid-1"
SUBJECT = "SENTINEL-subject-1"
EMAIL = "sentinel-email@example.test"
USERNAME = "SENTINEL-user"
FULL_NAME = "SENTINEL-full-name"
IDP_ACCESS_TOKEN = "SENTINEL-idp-access-token"
IDP_ERROR_TEXT = "SENTINEL-idp-error-description"

FAILED = "/login?sso_error=failed"
CANCELLED = "/login?sso_error=cancelled"
UNAVAILABLE = "/login?sso_error=unavailable"
NOT_ENABLED = {"detail": "Single sign-on is not enabled"}
EXCHANGE_REFUSED = {"detail": "Single sign-on session expired; sign in again"}

DROP = object()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _public_jwk(private_key, kid: str) -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    return jwk


@pytest.fixture(scope="module")
def keys() -> dict:
    return {
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "attacker": rsa.generate_private_key(public_exponent=65537, key_size=2048),
    }


class FakeIdP:
    """Discovery, JWKS, and a token endpoint. The token endpoint answers the ID
    token `token_factory` builds, by default a valid RS256 token carrying the
    nonce of the last authorization URL `begin` saw."""

    def __init__(self, keys: dict):
        self.keys = keys
        self.discovery: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }
        self.jwks = {"keys": [_public_jwk(keys["rsa"], KID)]}
        self.nonce: str | None = None
        self.claim_changes: dict[str, Any] = {}
        self.token_factory: Callable[[FakeIdP], str] = lambda idp: idp.signed()
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.token_requests: list[httpx.Request] = []
        self.counts = {"discovery": 0, "jwks": 0, "token": 0, "other": 0}
        # Every secret or provider value a test saw; none may reach a log record.
        self.secrets: list[str] = [
            CLIENT_SECRET,
            CODE,
            KID,
            SUBJECT,
            EMAIL,
            USERNAME,
            FULL_NAME,
            IDP_ACCESS_TOKEN,
            IDP_ERROR_TEXT,
            "SENTINEL",
        ]

    def claims(self, **changes) -> dict:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": SUBJECT,
            "exp": now + 300,
            "iat": now,
            "nonce": self.nonce,
            "email": EMAIL,
            "email_verified": True,
            "preferred_username": USERNAME,
            "name": FULL_NAME,
        }
        claims.update(self.claim_changes)
        claims.update(changes)
        return {name: value for name, value in claims.items() if value is not DROP}

    def signed(self, *, key: str = "rsa", kid: str = KID, headers=None, **changes) -> str:
        return jwt.encode(
            self.claims(**changes),
            self.keys[key],
            algorithm="RS256",
            headers={"kid": kid, **(headers or {})},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = {DISCOVERY_URL: "discovery", JWKS_URI: "jwks", TOKEN_ENDPOINT: "token"}.get(
            str(request.url), "other"
        )
        self.counts[name] += 1
        if name == "token":
            self.token_requests.append(request)
        if name in self.overrides:
            return self.overrides[name](request)
        if name == "discovery":
            return httpx.Response(200, json=self.discovery)
        if name == "jwks":
            return httpx.Response(200, json=self.jwks)
        if name == "token":
            id_token = self.token_factory(self)
            self.secrets.append(id_token)
            return httpx.Response(
                200,
                json={
                    "access_token": IDP_ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "id_token": id_token,
                },
            )
        return httpx.Response(404)


def _test_harness_record(record: logging.LogRecord) -> bool:
    """A record the test harness writes, not auth: aiosqlite (the in-memory test
    database's driver) logs every statement's parameters at DEBUG, where
    production runs asyncpg, which does not, with sqlalchemy.engine held at
    WARNING by herd_common.logging.setup_logging; and httpx logs each request
    the TEST client sends to the app (`http://test/...`, the browser's side of
    the callback URL). httpx records of auth's own calls to the provider, and
    HERD's access log (`herd.access`, path only), are checked."""
    if record.name == "aiosqlite":
        return True
    return record.name == "httpx" and " http://test/" in record.getMessage()


def _findings(caplog, needles: list[str]) -> list[str]:
    """Secrets found in the records of the test's setup and call phases. Read in
    the fixture's teardown, where `caplog.records` holds the teardown phase only."""
    formatter = JSONFormatter("auth")
    phases = [*caplog.get_records("setup"), *caplog.get_records("call")]
    assert phases, "no log record reached the hygiene check"
    texts = [formatter.format(record) for record in phases if not _test_harness_record(record)]
    return [f"{n!r} in {t[:160]}" for t in texts for n in needles if n and n in t]


@pytest.fixture
def idp(monkeypatch, keys, caplog):
    """OIDC mode, complete settings, the fake provider on the transport seam,
    empty caches, and the log-hygiene check over every record of the test."""
    caplog.set_level(logging.DEBUG)
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
        "oidc_require_https": True,
        "oidc_ca_cert": "",
        "oidc_provider_name": "Example IdP",
        "oidc_break_glass_local_superadmin": False,
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    fake = FakeIdP(keys)
    monkeypatch.setattr(oidc_service, "transport", httpx.MockTransport(fake.handler))
    oidc_service.reset_caches()
    yield fake
    oidc_service.reset_caches()
    assert _findings(caplog, fake.secrets) == []


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


# --- HTTP helpers ---


def set_cookies(response: httpx.Response) -> dict[str, dict]:
    """Each Set-Cookie header by cookie name: `value` plus every attribute,
    lower-cased, with a flag attribute mapped to True."""
    out: dict[str, dict] = {}
    for header in response.headers.get_list("set-cookie"):
        first, *attributes = [part.strip() for part in header.split(";")]
        name, _, value = first.partition("=")
        parsed: dict[str, Any] = {"value": value}
        for attribute in attributes:
            key, sep, attr_value = attribute.partition("=")
            parsed[key.lower()] = attr_value if sep else True
        out[name] = parsed
    return out


def assert_flow_cookie_cleared(response: httpx.Response) -> None:
    cookie = set_cookies(response)[oidc_router.FLOW_COOKIE]
    assert cookie["value"] == '""'
    assert cookie["max-age"] == "0"
    assert cookie["path"] == COOKIE_PATH
    assert cookie["httponly"] is True and cookie["secure"] is True
    assert cookie["samesite"] == "lax"


def cookie_header(**cookies: str) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}


@dataclass
class Begun:
    response: httpx.Response
    state: str
    nonce: str
    code_challenge: str
    binding: str
    query: dict[str, list[str]] = field(default_factory=dict)


async def begin(client, idp: FakeIdP, *, next_path: str | None = None, headers=None) -> Begun:
    url = "/oidc/login" + (f"?{urlencode({'next': next_path})}" if next_path is not None else "")
    response = await client.get(url, headers=headers or {})
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    begun = Begun(
        response=response,
        state=query["state"][0],
        nonce=query["nonce"][0],
        code_challenge=query["code_challenge"][0],
        binding=set_cookies(response)[oidc_router.FLOW_COOKIE]["value"],
        query=query,
    )
    idp.nonce = begun.nonce
    idp.secrets += [location, begun.state, begun.nonce, begun.binding]
    return begun


async def callback(client, params: dict[str, str], binding: str | None) -> httpx.Response:
    headers = cookie_header(herd_oidc_flow=binding) if binding is not None else {}
    response = await client.get("/oidc/callback?" + urlencode(params), headers=headers)
    assert response.status_code == 302, response.text
    assert response.headers["cache-control"] == "no-store"
    assert_flow_cookie_cleared(response)
    return response


async def complete_callback(client, idp: FakeIdP, begun: Begun, **extra) -> httpx.Response:
    return await callback(client, {"code": CODE, "state": begun.state, **extra}, begun.binding)


def handoff_of(response: httpx.Response, idp: FakeIdP) -> str:
    value = set_cookies(response)[oidc_router.HANDOFF_COOKIE]["value"]
    idp.secrets.append(value)
    return value


async def exchange(
    client, handoff: str | None, *, content=b"{}", content_type: str | None = "application/json"
) -> httpx.Response:
    headers = cookie_header(herd_oidc_handoff=handoff) if handoff is not None else {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return await client.post("/oidc/exchange", content=content, headers=headers)


async def sign_in(client, idp: FakeIdP, *, next_path: str | None = None):
    begun = await begin(client, idp, next_path=next_path)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == "/login/sso"
    return begun, response, handoff_of(response, idp)


async def flow_row(state: str) -> OidcLoginFlow | None:
    async with TestSessionLocal() as db:
        return (
            await db.execute(
                select(OidcLoginFlow).where(OidcLoginFlow.state_hash == hash_token(state))
            )
        ).scalar_one_or_none()


async def counts() -> tuple[int, int, int]:
    async with TestSessionLocal() as db:
        users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        identities = (
            await db.execute(select(func.count()).select_from(FederatedIdentity))
        ).scalar_one()
        refresh = (await db.execute(select(func.count()).select_from(RefreshToken))).scalar_one()
    return users, identities, refresh


def shift_flow_clock(monkeypatch, seconds: int) -> None:
    real = oidc_flow_service._utcnow
    monkeypatch.setattr(oidc_flow_service, "_utcnow", lambda: real() + timedelta(seconds=seconds))


def records(caplog, action: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "action", None) == action]


# --- dark outside OIDC mode ---


DARK_REQUESTS = {
    "login": ("GET", "/oidc/login?next=%2F%2Fevil.example", None, {}),
    "callback": (
        "GET",
        "/oidc/callback?state=s&code=c&error=access_denied",
        None,
        cookie_header(herd_oidc_flow="x"),
    ),
    "exchange": ("POST", "/oidc/exchange", b"{}", {"Content-Type": "application/json"}),
    "exchange-not-json": ("POST", "/oidc/exchange", b"{", {"Content-Type": "application/json"}),
    "exchange-not-empty": (
        "POST",
        "/oidc/exchange",
        b'{"a": 1}',
        {"Content-Type": "application/json"},
    ),
    "exchange-no-body": ("POST", "/oidc/exchange", None, {}),
}


@pytest.mark.parametrize("mode", ["local", "ldap"])
@pytest.mark.parametrize("request_name", sorted(DARK_REQUESTS))
async def test_every_route_answers_404_outside_oidc_mode_before_reading_input(
    monkeypatch, client, mode, request_name
):
    """The ADR's per-mode table: outside OIDC mode the three routes answer 404 with
    the fixed detail, set no cookie, open no database session, and call nothing in
    the protocol or flow-store modules, whatever the request carries."""
    monkeypatch.setattr(settings, "auth_method", mode)

    async def no_db():
        raise AssertionError("a database session was opened outside OIDC mode")
        yield  # pragma: no cover

    def forbidden(*args, **kwargs):
        raise AssertionError("OIDC code ran outside OIDC mode")

    app.dependency_overrides[get_db] = no_db
    for module, names in (
        (oidc_service, ["build_authorization_request", "redeem_code"]),
        (oidc_flow_service, ["start_flow", "claim_flow", "complete_flow", "fail_flow"]),
        (oidc_flow_service, ["exchange_handoff"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    method, url, content, headers = DARK_REQUESTS[request_name]
    response = await client.request(method, url, content=content, headers=headers)
    assert response.status_code == 404
    assert response.json() == NOT_ENABLED
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == []


# --- GET /oidc/login ---


async def test_begin_redirects_to_the_provider_and_sets_the_flow_cookie(client, idp):
    begun = await begin(client, idp, next_path="/reservations")
    response = begun.response
    assert response.headers["cache-control"] == "no-store"
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZATION_ENDPOINT
    query = {name: values for name, values in begun.query.items()}
    assert sorted(query) == sorted(oidc_service.AUTHORIZATION_PARAMETERS)
    assert all(len(values) == 1 for values in query.values())
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_mode"] == ["query"]
    assert "next" not in location.query and "reservations" not in location.query

    cookie = set_cookies(response)[oidc_router.FLOW_COOKIE]
    assert len(cookie["value"]) == 43
    assert cookie == {
        "value": cookie["value"],
        "httponly": True,
        "max-age": "600",
        "path": COOKIE_PATH,
        "samesite": "lax",
        "secure": True,
    }
    row = await flow_row(begun.state)
    assert row.status == "pending"
    assert row.binding_hash == hash_token(begun.binding)
    assert row.nonce == begun.nonce
    assert row.next_path == "/reservations"
    challenge = _b64(hashlib.sha256(row.code_verifier.encode()).digest())
    assert challenge == begun.code_challenge
    assert idp.counts["token"] == 0


@pytest.mark.parametrize(
    "redirect_uri,path",
    [
        ("https://herd.test/oidc/callback", "/oidc"),
        ("https://herd.test/herd/api/auth/oidc/callback", "/herd/api/auth/oidc"),
        ("https://herd.test:8443/api/auth/oidc/callback", "/api/auth/oidc"),
    ],
)
async def test_both_cookies_take_their_path_from_the_redirect_uri(
    monkeypatch, client, idp, redirect_uri, path
):
    monkeypatch.setattr(settings, "oidc_redirect_uri", redirect_uri)
    begun = await begin(client, idp)
    assert set_cookies(begun.response)[oidc_router.FLOW_COOKIE]["path"] == path
    response = await client.get(
        f"/oidc/callback?code={CODE}&state={begun.state}",
        headers=cookie_header(herd_oidc_flow=begun.binding),
    )
    cookies = set_cookies(response)
    handoff_of(response, idp)
    assert cookies[oidc_router.FLOW_COOKIE]["path"] == path
    assert cookies[oidc_router.HANDOFF_COOKIE]["path"] == path


async def test_begin_passes_the_raw_next_to_the_flow_store(monkeypatch, client, idp):
    """Carried requirement 8: the route hands `next` to start_flow untouched (the
    store sanitizes); the exchange answers only the stored, sanitized value."""
    seen = {}
    real = oidc_flow_service.start_flow

    async def spy(db, **kwargs):
        seen["next_path"] = kwargs["next_path"]
        return await real(db, **kwargs)

    monkeypatch.setattr(oidc_flow_service, "start_flow", spy)
    raw = "//evil.example/steal"
    _, _, handoff = await sign_in(client, idp, next_path=raw)
    assert seen["next_path"] == raw
    response = await exchange(client, handoff)
    idp.secrets += [response.json()["access_token"], response.json()["refresh_token"]]
    assert response.json()["next"] == "/topology"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("oidc_issuer", ""),
        ("oidc_client_secret", ""),
        ("oidc_client_id", " "),
        ("oidc_redirect_uri", "https://herd.test/api/auth/oidc/other"),
        ("oidc_redirect_uri", "https://herd.test/x;Domain=evil.example/oidc/callback"),
        ("oidc_redirect_uri", "https://herd.test/a,b/oidc/callback"),
        ("oidc_redirect_uri", "http://herd.test/api/auth/oidc/callback"),
    ],
    ids=["issuer", "secret", "client-id", "path", "cookie-injection", "comma", "http"],
)
async def test_begin_with_incomplete_settings_answers_unavailable(
    monkeypatch, client, idp, setting, value
):
    monkeypatch.setattr(settings, setting, value)
    response = await client.get("/oidc/login")
    assert response.status_code == 302
    assert response.headers["location"] == UNAVAILABLE
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == []
    assert sum(idp.counts.values()) == 0
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(OidcLoginFlow))).scalar_one() == 0


def _discovery_raises(request):
    raise httpx.ConnectError("SENTINEL-connect", request=request)


DISCOVERY_FAILURES = {
    "status-500": lambda idp: idp.overrides.update(discovery=lambda r: httpx.Response(500)),
    "transport-error": lambda idp: idp.overrides.update(discovery=_discovery_raises),
    "not-json": lambda idp: idp.overrides.update(
        discovery=lambda r: httpx.Response(200, content=b"<html>")
    ),
    "issuer-mismatch": lambda idp: idp.discovery.update(issuer=ISSUER + "/"),
    "pkce-downgrade": lambda idp: idp.discovery.update(code_challenge_methods_supported=["plain"]),
    "no-code-flow": lambda idp: idp.discovery.update(response_types_supported=["id_token"]),
    "http-endpoint": lambda idp: idp.discovery.update(
        authorization_endpoint="http://idp.test/auth"
    ),
}


@pytest.mark.parametrize("failure", sorted(DISCOVERY_FAILURES))
async def test_begin_when_discovery_fails_answers_unavailable(client, idp, failure):
    DISCOVERY_FAILURES[failure](idp)
    response = await client.get("/oidc/login")
    assert response.headers["location"] == UNAVAILABLE
    assert response.headers.get_list("set-cookie") == []
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(OidcLoginFlow))).scalar_one() == 0


async def test_begin_maps_a_flow_store_value_error_to_unavailable(monkeypatch, client, idp, caplog):
    """Carried requirement 7."""

    async def refuse(db, **kwargs):
        raise ValueError("SENTINEL-value-error")

    monkeypatch.setattr(oidc_flow_service, "start_flow", refuse)
    response = await client.get("/oidc/login")
    assert response.headers["location"] == UNAVAILABLE
    assert response.headers.get_list("set-cookie") == []
    [record] = records(caplog, "oidc_login_refused")
    assert record.reason == "flow_store_refused"


@pytest.mark.parametrize("target", ["build_authorization_request", "start_flow"])
async def test_begin_catch_all_answers_unavailable_and_logs_the_class_only(
    monkeypatch, client, idp, caplog, target
):
    """Carried requirement 3, the begin half: any unexpected exception becomes
    `unavailable`, logged once by class name, with no traceback and no message."""

    async def boom(*args, **kwargs):
        raise RuntimeError("SENTINEL-unexpected-message")

    module = oidc_service if target == "build_authorization_request" else oidc_flow_service
    monkeypatch.setattr(module, target, boom)
    response = await client.get("/oidc/login")
    assert response.headers["location"] == UNAVAILABLE
    assert response.headers.get_list("set-cookie") == []
    [record] = records(caplog, "oidc_internal_error")
    assert record.step == "login"
    assert record.error_class == "RuntimeError"
    assert record.exc_info is None and record.exc_text is None


async def test_begin_prunes_rows_expired_more_than_an_hour(client, idp):
    async with TestSessionLocal() as db:
        db.add(
            OidcLoginFlow(
                state_hash="old-state",
                binding_hash="old-binding",
                nonce="n",
                code_verifier=None,
                next_path="/topology",
                status="pending",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        await db.commit()
    await begin(client, idp)
    async with TestSessionLocal() as db:
        states = (await db.execute(select(OidcLoginFlow.state_hash))).scalars().all()
    assert "old-state" not in states and len(states) == 1


async def test_no_url_is_derived_from_request_headers(client, idp):
    """D13, host-header injection: forged Host and X-Forwarded headers change no URL."""
    forged = {
        "Host": "evil.example",
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-Proto": "http",
        "X-Forwarded-Prefix": "/evil",
    }
    begun = await begin(client, idp, headers=forged)
    assert urlsplit(begun.response.headers["location"]).netloc == "idp.test"
    assert begun.query["redirect_uri"] == [REDIRECT_URI]
    assert set_cookies(begun.response)[oidc_router.FLOW_COOKIE]["path"] == COOKIE_PATH
    response = await client.get(
        f"/oidc/callback?code={CODE}&state={begun.state}",
        headers={**forged, **cookie_header(herd_oidc_flow=begun.binding)},
    )
    handoff_of(response, idp)
    assert response.headers["location"] == "/login/sso"
    form = parse_qs(idp.token_requests[0].content.decode())
    assert form["redirect_uri"] == [REDIRECT_URI]


# --- the full flow ---


async def test_full_sign_in_reaches_me_and_refresh(client, idp):
    begun = await begin(client, idp, next_path="/reservations?tab=mine")
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == "/login/sso"
    handoff = handoff_of(response, idp)
    cookie = set_cookies(response)[oidc_router.HANDOFF_COOKIE]
    assert len(handoff) == 43
    assert cookie == {
        "value": handoff,
        "httponly": True,
        "max-age": "60",
        "path": COOKIE_PATH,
        "samesite": "strict",
        "secure": True,
    }

    # The token request: the ADR's form, client_secret_basic, and the verifier
    # whose S256 challenge went to the provider (PKCE binds the code to this flow).
    [token_request] = idp.token_requests
    form = parse_qs(token_request.content.decode())
    verifier = form.pop("code_verifier")[0]
    idp.secrets.append(verifier)
    assert form == {
        "grant_type": ["authorization_code"],
        "code": [CODE],
        "redirect_uri": [REDIRECT_URI],
    }
    assert _b64(hashlib.sha256(verifier.encode()).digest()) == begun.code_challenge
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert token_request.headers["authorization"] == f"Basic {expected_basic}"
    assert (await flow_row(begun.state)).status == "authenticated"

    exchanged = await exchange(client, handoff)
    assert exchanged.status_code == 200
    assert exchanged.headers["cache-control"] == "no-store"
    body = exchanged.json()
    idp.secrets += [body["access_token"], body["refresh_token"]]
    assert sorted(body) == ["access_token", "next", "refresh_token", "token_type"]
    assert body["token_type"] == "bearer"
    assert body["next"] == "/reservations?tab=mine"
    cleared = set_cookies(exchanged)[oidc_router.HANDOFF_COOKIE]
    assert (cleared["value"], cleared["max-age"], cleared["path"]) == ('""', "0", COOKIE_PATH)
    assert cleared["samesite"] == "strict"
    assert (await flow_row(begun.state)).status == "exchanged"

    me = await client.get("/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert (me.json()["email"], me.json()["username"], me.json()["full_name"]) == (
        EMAIL,
        USERNAME,
        FULL_NAME,
    )
    assert me.json()["role"] == "user"
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User))).scalar_one()
        assert (user.auth_source, user.hashed_password) == ("oidc", None)

    refreshed = await client.post("/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200
    idp.secrets += [refreshed.json()["access_token"], refreshed.json()["refresh_token"]]


async def test_a_returning_login_reuses_the_account(client, idp):
    for _ in range(2):
        _, _, handoff = await sign_in(client, idp)
        body = (await exchange(client, handoff)).json()
        idp.secrets += [body["access_token"], body["refresh_token"]]
    users, identities, _ = await counts()
    assert (users, identities) == (1, 1)


async def test_the_claimed_flows_nonce_and_verifier_reach_redeem_code(monkeypatch, client, idp):
    """Carried requirement 10: the callback redeems with the claimed row's nonce and
    verifier, not values from the request."""
    seen = {}
    real = oidc_service.redeem_code

    async def spy(code, code_verifier, nonce):
        seen.update(code=code, code_verifier=code_verifier, nonce=nonce)
        return await real(code, code_verifier, nonce)

    monkeypatch.setattr(oidc_service, "redeem_code", spy)
    begun = await begin(client, idp)
    stored_verifier = (await flow_row(begun.state)).code_verifier
    idp.secrets.append(stored_verifier)
    response = await complete_callback(client, idp, begun, nonce="SENTINEL-forged-nonce")
    handoff_of(response, idp)
    assert seen == {"code": CODE, "code_verifier": stored_verifier, "nonce": begun.nonce}


# --- the callback outcome table: rows before the claim ---


async def _pre_claim_case(case: str, client, idp, monkeypatch) -> tuple[dict, str | None, Begun]:
    begun = await begin(client, idp)
    params = {"code": CODE, "state": begun.state}
    binding: str | None = begun.binding
    if case == "state-missing":
        del params["state"]
    elif case == "state-empty":
        params["state"] = ""
    elif case == "state-unknown":
        params["state"] = "SENTINEL-unknown-state-" + "x" * 30
    elif case == "flow-expired":
        shift_flow_clock(monkeypatch, 601)
    elif case == "cookie-missing":
        binding = None
    elif case == "cookie-differs":
        binding = "SENTINEL-other-binding-" + "y" * 30
    return params, binding, begun


@pytest.mark.parametrize(
    "case",
    [
        "state-missing",
        "state-empty",
        "state-unknown",
        "flow-expired",
        "cookie-missing",
        "cookie-differs",
    ],
)
async def test_callback_before_the_claim_answers_failed(monkeypatch, client, idp, case):
    params, binding, begun = await _pre_claim_case(case, client, idp, monkeypatch)
    response = await callback(client, params, binding)
    assert response.headers["location"] == FAILED
    assert oidc_router.HANDOFF_COOKIE not in set_cookies(response)
    assert idp.counts["token"] == 0
    # Nothing was claimed, so nothing moved: the row is still pending.
    assert (await flow_row(begun.state)).status == "pending"


async def test_a_second_begin_replaces_the_cookie_and_the_first_tab_fails(client, idp):
    first = await begin(client, idp)
    second = await begin(client, idp)
    response = await callback(client, {"code": CODE, "state": first.state}, second.binding)
    assert response.headers["location"] == FAILED
    assert (await flow_row(first.state)).status == "pending"
    response = await callback(client, {"code": CODE, "state": second.state}, second.binding)
    handoff_of(response, idp)
    assert response.headers["location"] == "/login/sso"


async def test_login_csrf_with_the_attackers_state_fails_in_the_victims_browser(client, idp):
    """D13, login CSRF and code injection: the attacker's own code and state, sent
    to a victim whose browser holds its own binding cookie, cannot be claimed."""
    attacker = await begin(client, idp)
    victim = await begin(client, idp)
    response = await callback(client, {"code": CODE, "state": attacker.state}, victim.binding)
    assert response.headers["location"] == FAILED
    assert idp.counts["token"] == 0
    assert (await flow_row(attacker.state)).status == "pending"
    assert (await flow_row(victim.state)).status == "pending"


async def test_a_replayed_callback_fails_and_never_redeems_the_code_twice(client, idp):
    """D13, state replay: the second callback loses the pending-to-claimed swap."""
    begun, _, handoff = await sign_in(client, idp)
    replay = await complete_callback(client, idp, begun)
    assert replay.headers["location"] == FAILED
    assert idp.counts["token"] == 1
    assert (await flow_row(begun.state)).status == "authenticated"


# --- the callback outcome table: rows after the claim ---


@pytest.mark.parametrize(
    "params,location",
    [
        ({"error": "access_denied"}, CANCELLED),
        ({"error": "access_denied", "error_description": IDP_ERROR_TEXT}, CANCELLED),
        ({"error": "access_denied", "code": CODE}, CANCELLED),
        ({"error": "server_error", "code": CODE}, FAILED),
        ({"error": "SENTINEL-not-registered", "error_description": IDP_ERROR_TEXT}, FAILED),
        ({"error": ""}, FAILED),
        ({}, FAILED),
        ({"code": ""}, FAILED),
    ],
    ids=[
        "access-denied",
        "access-denied-description",
        "access-denied-with-code",
        "server-error",
        "unregistered-error",
        "empty-error",
        "no-code-no-error",
        "empty-code",
    ],
)
async def test_callback_error_and_code_rows(client, idp, params, location):
    begun = await begin(client, idp)
    response = await callback(client, {"state": begun.state, **params}, begun.binding)
    assert response.headers["location"] == location
    assert oidc_router.HANDOFF_COOKIE not in set_cookies(response)
    assert idp.counts["token"] == 0
    assert (await flow_row(begun.state)).status == "failed"


@pytest.mark.parametrize(
    "iss,location",
    [
        (ISSUER, "/login/sso"),
        (ISSUER + "/", FAILED),
        ("", FAILED),
        ("https://evil.example/realms/herd", FAILED),
        (ISSUER.upper(), FAILED),
    ],
    ids=["exact", "trailing-slash", "empty", "other-issuer", "case"],
)
async def test_the_rfc_9207_iss_parameter_must_equal_the_issuer(client, idp, iss, location):
    """Carried requirement 1: when the callback carries `iss`, it is compared exactly."""
    begun = await begin(client, idp)
    response = await complete_callback(client, idp, begun, iss=iss)
    assert response.headers["location"] == location
    if location == FAILED:
        assert idp.counts["token"] == 0
        assert (await flow_row(begun.state)).status == "failed"
    else:
        handoff_of(response, idp)


def _raise(error_class):
    def answer(request):
        raise error_class("SENTINEL-transport", request=request)

    return answer


UNAVAILABLE_AFTER_CLAIM = {
    "settings-incomplete": lambda idp, mp: mp.setattr(settings, "oidc_client_secret", ""),
    "discovery-unreachable": lambda idp, mp: (
        oidc_service.reset_caches(),
        idp.overrides.update(discovery=_raise(httpx.ConnectError)),
    ),
    "jwks-unreachable": lambda idp, mp: idp.overrides.update(jwks=_raise(httpx.ConnectError)),
    "jwks-500": lambda idp, mp: idp.overrides.update(jwks=lambda r: httpx.Response(500)),
    "token-unreachable": lambda idp, mp: idp.overrides.update(token=_raise(httpx.ConnectError)),
    "token-timeout": lambda idp, mp: idp.overrides.update(token=_raise(httpx.ReadTimeout)),
    "token-not-json": lambda idp, mp: idp.overrides.update(
        token=lambda r: httpx.Response(200, content=b"SENTINEL-not-json")
    ),
    "token-503": lambda idp, mp: idp.overrides.update(token=lambda r: httpx.Response(503)),
}


@pytest.mark.parametrize("case", sorted(UNAVAILABLE_AFTER_CLAIM))
async def test_provider_unavailable_after_the_claim_answers_unavailable(
    monkeypatch, client, idp, case
):
    begun = await begin(client, idp)
    UNAVAILABLE_AFTER_CLAIM[case](idp, monkeypatch)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == UNAVAILABLE
    assert oidc_router.HANDOFF_COOKIE not in set_cookies(response)
    assert (await flow_row(begun.state)).status == "failed"


@pytest.mark.parametrize(
    "answer",
    [
        lambda r: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": IDP_ERROR_TEXT}
        ),
        lambda r: httpx.Response(401, json={"error": "invalid_client"}),
        lambda r: httpx.Response(200, json={"access_token": IDP_ACCESS_TOKEN}),
        lambda r: httpx.Response(200, json={"id_token": ""}),
    ],
    ids=["invalid-grant", "invalid-client", "no-id-token", "empty-id-token"],
)
async def test_token_endpoint_refusals_answer_failed(client, idp, answer):
    begun = await begin(client, idp)
    idp.overrides["token"] = answer
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == FAILED
    assert (await flow_row(begun.state)).status == "failed"


def _unsigned(idp: FakeIdP) -> str:
    header = _b64(json.dumps({"alg": "none", "kid": KID}).encode())
    return f"{header}.{_b64(json.dumps(idp.claims()).encode())}."


TOKEN_REFUSALS: dict[str, Callable[[FakeIdP], str]] = {
    "nonce-of-another-flow": lambda idp: idp.signed(nonce="SENTINEL-another-nonce"),
    "nonce-missing": lambda idp: idp.signed(nonce=DROP),
    "audience-another-client": lambda idp: idp.signed(aud="another-client"),
    "two-audiences-no-azp": lambda idp: idp.signed(aud=[CLIENT_ID, "another-client"]),
    "azp-another-client": lambda idp: idp.signed(azp="another-client"),
    "token-issuer-mismatch": lambda idp: idp.signed(iss=ISSUER + "/other"),
    "expired": lambda idp: idp.signed(exp=int(time.time()) - 120, iat=int(time.time()) - 600),
    "not-yet-valid": lambda idp: idp.signed(nbf=int(time.time()) + 300),
    "alg-none": _unsigned,
    "hs256-keyed-by-client-secret": lambda idp: jwt.encode(
        idp.claims(), CLIENT_SECRET, algorithm="HS256", headers={"kid": KID}
    ),
    "unknown-kid": lambda idp: idp.signed(kid="SENTINEL-unknown-kid"),
    "signed-by-another-key": lambda idp: idp.signed(key="attacker"),
    "access-token-type": lambda idp: idp.signed(headers={"typ": "at+jwt"}),
    "subject-missing": lambda idp: idp.signed(sub=DROP),
}


@pytest.mark.parametrize("case", sorted(TOKEN_REFUSALS))
async def test_every_id_token_refusal_answers_failed(client, idp, case):
    """D13 over HTTP: nonce binding, token substitution, issuer, expiry, algorithm
    confusion, and key selection each refuse the sign-in."""
    idp.token_factory = TOKEN_REFUSALS[case]
    begun = await begin(client, idp)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == FAILED
    assert oidc_router.HANDOFF_COOKIE not in set_cookies(response)
    assert (await flow_row(begun.state)).status == "failed"
    assert await counts() == (0, 0, 0)


async def test_at_hash_is_not_checked_by_decision(client, idp):
    """D13: HERD never uses the provider's access token, so a wrong `at_hash` binds
    nothing HERD consumes and is accepted."""
    idp.claim_changes["at_hash"] = "not-the-hash-of-anything"
    _, _, handoff = await sign_in(client, idp)
    body = (await exchange(client, handoff)).json()
    idp.secrets += [body["access_token"], body["refresh_token"]]


async def _local_account(email: str = "owner@example.test", username: str = "owner") -> User:
    async with TestSessionLocal() as db:
        return await create_user(db, email, username, "local-password-1")


PROVISIONING_REFUSALS = {
    "email-missing": {"email": DROP},
    "email-unverified": {"email_verified": False},
    "email-taken-by-a-local-account": {"email": "OWNER@example.test"},
    "username-taken": {"preferred_username": "Owner"},
    "no-usable-username": {"preferred_username": "bad name", "email": "o'brien@example.test"},
}


@pytest.mark.parametrize("case", sorted(PROVISIONING_REFUSALS))
async def test_provisioning_refusals_answer_failed_and_link_nothing(client, idp, case):
    """D4 over HTTP, D13 account takeover through email: the local account is never
    linked or changed, and nothing is created."""
    local = await _local_account()
    idp.claim_changes.update(PROVISIONING_REFUSALS[case])
    begun = await begin(client, idp)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == FAILED
    assert (await flow_row(begun.state)).status == "failed"
    users, identities, _ = await counts()
    assert (users, identities) == (1, 0)
    async with TestSessionLocal() as db:
        stored = (await db.execute(select(User).where(User.id == local.id))).scalar_one()
        assert (stored.auth_source, stored.email, stored.username) == (
            "local",
            "owner@example.test",
            "owner",
        )


async def test_an_inactive_linked_account_is_refused_at_the_callback(client, idp):
    _, _, handoff = await sign_in(client, idp)
    body = (await exchange(client, handoff)).json()
    idp.secrets += [body["access_token"], body["refresh_token"]]
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User))).scalar_one()
        await set_user_active(db, user.id, False)
    begun = await begin(client, idp)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == FAILED
    assert (await flow_row(begun.state)).status == "failed"


@pytest.mark.parametrize(
    "claim,email,username",
    [
        ("alice.smith", "alice@example.test", "alice.smith"),
        ("\u0430lice", "alice@example.test", "alice@example.test"),
        (None, "alice@example.test", "alice@example.test"),
        ("u" * 101, "alice@example.test", "alice@example.test"),
    ],
    ids=["claim-fits", "cyrillic-lookalike-claim", "claim-absent", "claim-101"],
)
async def test_option_c_username_over_http(client, idp, claim, email, username):
    idp.claim_changes.update(preferred_username=DROP if claim is None else claim, email=email)
    idp.secrets.append(email)
    _, _, handoff = await sign_in(client, idp)
    body = (await exchange(client, handoff)).json()
    idp.secrets += [body["access_token"], body["refresh_token"]]
    async with TestSessionLocal() as db:
        assert (await db.execute(select(User.username))).scalar_one() == username


def test_federated_login_from_token_maps_every_field():
    verified = VerifiedIdToken(
        issuer=ISSUER,
        subject="sub-1",
        email="alice@example.test",
        email_verified=None,
        username_claim="alice",
        name="n" * 250,
    )
    login = oidc_router.federated_login_from_token(verified)
    assert login == auth_service.FederatedLogin(
        issuer=ISSUER,
        subject="sub-1",
        email="alice@example.test",
        email_verified=None,
        username="alice",
        full_name="n" * 200,
    )
    nameless = oidc_router.federated_login_from_token(
        VerifiedIdToken(ISSUER, "sub-1", "alice@example.test", True, None, None)
    )
    assert (nameless.username, nameless.full_name) == ("alice@example.test", "alice@example.test")
    hopeless = oidc_router.federated_login_from_token(
        VerifiedIdToken(ISSUER, "sub-1", "o'brien@example.test", True, "bad name", None)
    )
    assert (hopeless.username, hopeless.full_name) == (None, None)


async def test_complete_flow_false_fails_the_row_and_answers_failed(monkeypatch, client, idp):
    """Carried requirement 6."""
    real_fail = oidc_flow_service.fail_flow
    failed = []

    async def refuse(db, flow_id, **kwargs):
        await db.rollback()
        return False

    async def spy_fail(db, flow_id):
        failed.append(flow_id)
        return await real_fail(db, flow_id)

    monkeypatch.setattr(oidc_flow_service, "complete_flow", refuse)
    monkeypatch.setattr(oidc_flow_service, "fail_flow", spy_fail)
    begun = await begin(client, idp)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == FAILED
    assert oidc_router.HANDOFF_COOKIE not in set_cookies(response)
    row = await flow_row(begun.state)
    assert row.status == "failed" and failed == [row.id]


async def test_the_user_id_is_read_before_complete_flow_can_expire_it(monkeypatch, client, idp):
    """Carried requirement 5: complete_flow ends its transaction, which can expire
    every ORM instance; here it expires them first. A route that read `user.id`
    afterwards would need a lazy load (MissingGreenlet in async) and answer
    `unavailable` through the catch-all instead of signing in."""
    real = oidc_flow_service.complete_flow

    async def expiring(db, flow_id, **kwargs):
        db.expire_all()
        return await real(db, flow_id, **kwargs)

    monkeypatch.setattr(oidc_flow_service, "complete_flow", expiring)
    _, _, handoff = await sign_in(client, idp)
    body = (await exchange(client, handoff)).json()
    idp.secrets += [body["access_token"], body["refresh_token"]]


@pytest.mark.parametrize(
    "module,target,claimed",
    [
        (oidc_flow_service, "claim_flow", False),
        (oidc_service, "redeem_code", True),
        (auth_service, "resolve_federated_user", True),
        (oidc_flow_service, "complete_flow", True),
    ],
    ids=["claim", "redeem", "resolve", "complete"],
)
async def test_callback_catch_all_answers_unavailable_and_logs_the_class_only(
    monkeypatch, client, idp, caplog, module, target, claimed
):
    """Carried requirement 3, the callback half: the claimed row is failed, the
    answer is `unavailable`, and the log carries the class name only."""

    async def boom(*args, **kwargs):
        raise RuntimeError("SENTINEL-unexpected-message")

    begun = await begin(client, idp)
    monkeypatch.setattr(module, target, boom)
    response = await complete_callback(client, idp, begun)
    assert response.headers["location"] == UNAVAILABLE
    assert oidc_router.HANDOFF_COOKIE not in set_cookies(response)
    assert (await flow_row(begun.state)).status == ("failed" if claimed else "pending")
    [record] = records(caplog, "oidc_internal_error")
    assert (record.step, record.error_class) == ("callback", "RuntimeError")
    assert record.exc_info is None and record.exc_text is None


async def test_no_route_logs_the_authorization_url_or_a_verified_token(client, idp, caplog):
    """Carried requirement 4, beyond the substring check every test runs: no record
    carries the authorization URL, a VerifiedIdToken, or its repr, in its message,
    its arguments, or its extra."""
    begun, _, handoff = await sign_in(client, idp)
    body = (await exchange(client, handoff)).json()
    idp.secrets += [body["access_token"], body["refresh_token"]]
    location = begun.response.headers["location"]
    for record in caplog.records:
        values = [
            record.getMessage(),
            *map(str, record.args or ()),
            *map(repr, record.__dict__.values()),
        ]
        assert not any(isinstance(v, VerifiedIdToken) for v in record.__dict__.values())
        assert not any("VerifiedIdToken" in v or location in v for v in values)


# --- POST /oidc/exchange ---


async def test_exchange_without_a_cookie_answers_401_and_clears_it(client, idp):
    response = await exchange(client, None)
    assert response.status_code == 401
    assert response.json() == EXCHANGE_REFUSED
    assert response.headers["cache-control"] == "no-store"
    cleared = set_cookies(response)[oidc_router.HANDOFF_COOKIE]
    assert (cleared["value"], cleared["max-age"], cleared["path"]) == ('""', "0", COOKIE_PATH)


async def test_exchange_refusals_answer_401(monkeypatch, client, idp):
    """Unknown, already exchanged (D13 handoff replay), and expired handoffs."""
    unknown = await exchange(client, "SENTINEL-unknown-handoff-" + "z" * 30)
    assert (unknown.status_code, unknown.json()) == (401, EXCHANGE_REFUSED)

    _, _, handoff = await sign_in(client, idp)
    first = await exchange(client, handoff)
    idp.secrets += [first.json()["access_token"], first.json()["refresh_token"]]
    replay = await exchange(client, handoff)
    assert (replay.status_code, replay.json()) == (401, EXCHANGE_REFUSED)
    assert oidc_router.HANDOFF_COOKIE in set_cookies(replay)

    _, _, late = await sign_in(client, idp)
    shift_flow_clock(monkeypatch, 61)
    expired = await exchange(client, late)
    assert (expired.status_code, expired.json()) == (401, EXCHANGE_REFUSED)
    assert (await counts())[2] == 1


async def test_exchange_refused_for_a_user_deactivated_after_the_callback(client, idp):
    _, _, handoff = await sign_in(client, idp)
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User))).scalar_one()
        await set_user_active(db, user.id, False)
    response = await exchange(client, handoff)
    assert (response.status_code, response.json()) == (401, EXCHANGE_REFUSED)
    assert (await counts())[2] == 0


async def test_exchange_rechecks_the_user_after_the_handoff_is_consumed(
    monkeypatch, client, idp, caplog
):
    """Carried requirement 9: a deactivation landing after exchange_handoff's
    guarded update and before the tokens are minted still refuses."""
    real = oidc_flow_service.exchange_handoff

    async def deactivate_after(db, **kwargs):
        result = await real(db, **kwargs)
        async with TestSessionLocal() as other:
            await set_user_active(other, result[0], False)
        return result

    monkeypatch.setattr(oidc_flow_service, "exchange_handoff", deactivate_after)
    _, _, handoff = await sign_in(client, idp)
    response = await exchange(client, handoff)
    assert (response.status_code, response.json()) == (401, EXCHANGE_REFUSED)
    assert (await counts())[2] == 0
    [record] = records(caplog, "oidc_exchange_refused")
    assert record.reason == "user_inactive_after_exchange"


@pytest.mark.parametrize(
    "content,content_type",
    [
        (b"", "application/json"),
        (b"{", "application/json"),
        (b'{"next": "/admin"}', "application/json"),
        (b"[]", "application/json"),
        (b"null", "application/json"),
        (b"{}", None),
        (b"{}", "text/plain"),
        (b"{}", "application/x-www-form-urlencoded"),
    ],
    ids=[
        "empty",
        "not-json",
        "not-empty",
        "array",
        "null",
        "no-content-type",
        "text-plain",
        "form",
    ],
)
async def test_exchange_body_must_be_an_empty_json_object(client, idp, content, content_type):
    """422 (FastAPI's default body) for anything but `{}` sent as application/json;
    the handoff is not consumed, so the real exchange still succeeds afterwards."""
    _, _, handoff = await sign_in(client, idp)
    refused = await exchange(client, handoff, content=content, content_type=content_type)
    assert refused.status_code == 422
    assert isinstance(refused.json()["detail"], list)
    accepted = await exchange(client, handoff)
    assert accepted.status_code == 200
    idp.secrets += [accepted.json()["access_token"], accepted.json()["refresh_token"]]


async def test_exchange_accepts_a_charset_parameter(client, idp):
    _, _, handoff = await sign_in(client, idp)
    response = await exchange(client, handoff, content_type="application/json; charset=utf-8")
    assert response.status_code == 200
    idp.secrets += [response.json()["access_token"], response.json()["refresh_token"]]


async def test_exchange_never_answers_a_next_outside_the_origin(client, idp):
    """D13, open redirect: `next` is stored sanitized and only that value comes back."""
    for raw, expected in [
        ("https://evil.example/", "/topology"),
        ("/\\evil.example", "/topology"),
        ("/devices?x=1#frag", "/devices?x=1#frag"),
    ]:
        _, _, handoff = await sign_in(client, idp, next_path=raw)
        body = (await exchange(client, handoff)).json()
        idp.secrets += [body["access_token"], body["refresh_token"]]
        assert body["next"] == expected


def test_routes_use_the_fixed_constants():
    assert oidc_router.FLOW_COOKIE == "herd_oidc_flow"
    assert oidc_router.HANDOFF_COOKIE == "herd_oidc_handoff"
    assert (oidc_router.FLOW_COOKIE_MAX_AGE, oidc_router.HANDOFF_COOKIE_MAX_AGE) == (600, 60)
    assert oidc_router.error_location("failed") == FAILED
