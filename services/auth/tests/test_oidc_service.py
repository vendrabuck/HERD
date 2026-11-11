"""app.services.oidc_service, the OpenID Connect relying-party protocol (ADR 0016
phase 1, issue #37). Unit level: no route exists yet.

Every test runs against a fake identity provider behind `httpx.MockTransport`
(the module's `transport` seam) with a controlled clock (the `clock` seam); no
real network. Keys are generated per session; no key material is committed.

What is pinned:

- ID-token verification refuses a token when exactly one thing is wrong, one
  case per rule (`test_each_id_token_rule_refuses_a_token_otherwise_valid`),
  each case built from the token `test_the_baseline_token_verifies` accepts,
  plus the algorithm-confusion cases (`none`, HS keyed by the client secret or
  the public key PEM, another asymmetric algorithm, a key of the wrong type);
- discovery, JWKS, and the token endpoint fail closed, each failure class with
  its own reason, and no failure is cached (the next call fetches again);
- only `OidcUnavailableError` or `OidcRejectedError` ever leaves an entry point
  for provider-shaped input: `test_a_hostile_shape_yields_only_the_two_error_classes`
  feeds every endpoint every body shape and every field the code reads every
  hostile JSON value (856 cases, `test_the_hostile_shape_table_size`), each
  mapped to the outcome the documented rule names;
- each request is cut at the overall deadline even when the server never
  answers or stalls mid-body (`test_a_request_that_never_completes_is_cut_at_the_deadline`);
- discovery and JWKS are cached for 3600 s, and an unknown `kid` refetches the
  JWKS at most once per 60 s, concurrent misses included;
- the authorization URL carries exactly the ADR's parameters, with S256 PKCE
  and state, nonce, and verifier drawn from `secrets` at the ADR's sizes;
- the token request is the ADR's form with `client_secret_basic`, both halves
  form-urlencoded;
- log hygiene: the `idp` fixture formats EVERY log record a test produced
  through `JSONFormatter` and fails the test when any secret or provider text
  appears (client secret, code, verifier, nonce, state, ID token, IdP access
  token, error description, response body, claim values, key id), so every
  success and failure path below is also a hygiene test; each failure test
  also checks the exception's `str()` and `repr()`, that it has no cause, and
  that any context it was raised in is suppressed (`from None`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import ssl
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import jwt
import pytest
from app.config import settings
from app.services import oidc_service
from app.services.oidc_service import (
    AuthorizationRequest,
    OidcRejectedError,
    OidcUnavailableError,
    VerifiedIdToken,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID
from herd_common.logging import JSONFormatter
from jwt.warnings import InsecureKeyLengthWarning

ISSUER = "https://idp.test/realms/herd"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = ISSUER + "/protocol/openid-connect/auth"
TOKEN_ENDPOINT = ISSUER + "/protocol/openid-connect/token"
JWKS_URI = ISSUER + "/protocol/openid-connect/certs"
# A space and a slash, so form-urlencoding the Basic credentials is visible.
CLIENT_ID = "herd client/1"
REDIRECT_URI = "https://herd.test/api/auth/oidc/callback"

# Secret or provider-sent values. None may appear in any log record, and none
# but the claims may leave the module except inside the returned dataclass.
CLIENT_SECRET = "SENTINEL-client-secret:with+odd/chars%"
CODE = "SENTINEL-authorization-code"
VERIFIER = "SENTINEL-verifier-" + "v" * 46
NONCE = "SENTINEL-nonce-of-the-flow"
IDP_ACCESS_TOKEN = "SENTINEL-idp-access-token"
IDP_ERROR_TEXT = "SENTINEL-idp-error-description"
IDP_BODY_TEXT = "SENTINEL-idp-raw-body"
SUBJECT = "SENTINEL-subject-1"
EMAIL = "sentinel-email@example.test"
USERNAME = "SENTINEL-username"
FULL_NAME = "SENTINEL-full-name"
KID = "SENTINEL-kid-1"

SECRETS = [
    CLIENT_SECRET,
    CODE,
    VERIFIER,
    NONCE,
    IDP_ACCESS_TOKEN,
    IDP_ERROR_TEXT,
    IDP_BODY_TEXT,
    SUBJECT,
    EMAIL,
    USERNAME,
    FULL_NAME,
    KID,
    "SENTINEL",
]


# --- keys, tokens, and the fake provider ---


@pytest.fixture(scope="module")
def keys() -> dict:
    return {
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "rsa_rotated": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "attacker": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "rsa_1024": rsa.generate_private_key(public_exponent=65537, key_size=1024),
        "P-256": ec.generate_private_key(ec.SECP256R1()),
        "P-384": ec.generate_private_key(ec.SECP384R1()),
        "P-521": ec.generate_private_key(ec.SECP521R1()),
    }


def public_jwk(private_key, kid: str, **members) -> dict:
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
    else:
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(public))
    jwk["kid"] = kid
    jwk.update(members)
    return jwk


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def base_claims() -> dict:
    now = int(datetime.now(UTC).timestamp())
    return {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": SUBJECT,
        "exp": now + 300,
        "iat": now,
        "nonce": NONCE,
        "email": EMAIL,
        "email_verified": True,
        "preferred_username": USERNAME,
        "name": FULL_NAME,
    }


def signed_raw(keys: dict, payload: bytes, header: bytes | None = None) -> str:
    """A compact RS256 JWS over exact payload (and header) bytes, signed with the
    provider's key, for shapes `jwt.encode` cannot produce."""
    if header is None:
        header = json.dumps({"alg": "RS256", "typ": "JWT", "kid": KID}).encode()
    signing_input = f"{_b64(header)}.{_b64(payload)}".encode()
    signature = keys["rsa"].sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64(signature)}"


@dataclass
class TokenSpec:
    """How to build one ID token; the default is a valid RS256 token."""

    claims: dict = field(default_factory=base_claims)
    kid: str | None = KID
    algorithm: str = "RS256"
    signer: str = "rsa"
    hmac_secret: bytes | None = None
    unsigned: bool = False
    tamper_sub: bool = False
    header_extra: dict = field(default_factory=dict)
    raw: str | None = None

    def build(self, keys: dict) -> str:
        if self.raw is not None:
            return self.raw
        header = {"alg": self.algorithm, "typ": "JWT"}
        if self.kid is not None:
            header["kid"] = self.kid
        if self.unsigned or self.hmac_secret is not None:
            if self.unsigned:
                header["alg"] = "none"
            head = _b64(json.dumps(header).encode())
            body = _b64(json.dumps(self.claims).encode())
            if self.unsigned:
                return f"{head}.{body}."
            digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}
            mac = hmac.new(
                self.hmac_secret, f"{head}.{body}".encode(), digest[self.algorithm]
            ).digest()
            return f"{head}.{body}.{_b64(mac)}"
        extra_headers = {"kid": self.kid} if self.kid is not None else {}
        extra_headers.update(self.header_extra)
        if self.signer == "rsa_1024":
            with pytest.warns(InsecureKeyLengthWarning):
                token = jwt.encode(
                    self.claims, keys[self.signer], algorithm=self.algorithm, headers=extra_headers
                )
        else:
            token = jwt.encode(
                self.claims, keys[self.signer], algorithm=self.algorithm, headers=extra_headers
            )
        if self.tamper_sub:
            head, _, signature = token.split(".")
            forged = dict(self.claims, sub="SENTINEL-someone-else")
            token = f"{head}.{_b64(json.dumps(forged).encode())}.{signature}"
        return token


def json_response(document: Any, status: int = 200) -> httpx.Response:
    """A JSON answer encoded with json.dumps (ASCII-escaped), so a lone surrogate
    in a test document reaches the code under test instead of failing here."""
    return httpx.Response(
        status, content=json.dumps(document).encode(), headers={"content-type": "application/json"}
    )


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeIdP:
    """The provider's three endpoints. `overrides[name]` replaces an endpoint's
    answer with a callable taking the request (it may raise a transport error)."""

    def __init__(self, keys: dict):
        self.keys = keys
        self.discovery: Any = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
            "end_session_endpoint": ISSUER + "/protocol/openid-connect/logout",
            "response_types_supported": ["code", "id_token", "code id_token"],
            "code_challenge_methods_supported": ["plain", "S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        self.jwks: Any = {"keys": [public_jwk(keys["rsa"], KID)]}
        self.id_token: str | None = None
        self.overrides: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.counts = {"discovery": 0, "jwks": 0, "token": 0, "other": 0}

    def use_token(self, spec: TokenSpec | None = None) -> str:
        self.id_token = (spec or TokenSpec()).build(self.keys)
        return self.id_token

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = {DISCOVERY_URL: "discovery", JWKS_URI: "jwks", TOKEN_ENDPOINT: "token"}.get(
            str(request.url), "other"
        )
        self.counts[name] += 1
        if name in self.overrides:
            return self.overrides[name](request)
        if name == "discovery":
            return json_response(self.discovery)
        if name == "jwks":
            return json_response(self.jwks)
        if name == "token":
            # json.dumps escapes non-ASCII (a lone surrogate included) as httpx's
            # json= would not.
            body = {
                "access_token": IDP_ACCESS_TOKEN,
                "token_type": "Bearer",
                "id_token": self.id_token,
            }
            return httpx.Response(
                200,
                content=json.dumps(body).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, text=IDP_BODY_TEXT)


def secret_findings(texts: list[str], extra: list[str] = ()) -> list[str]:
    needles = SECRETS + [s for s in extra if s]
    return [f"{needle!r} in {text}" for text in texts for needle in needles if needle in text]


@pytest.fixture
def idp(monkeypatch, keys, caplog):
    """OIDC mode with a complete configuration, the fake provider on the
    transport seam, a controlled clock, empty caches, and the log check."""
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
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    fake = FakeIdP(keys)
    fake.clock = FakeClock()
    fake.extra_secrets = []
    monkeypatch.setattr(oidc_service, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(oidc_service, "clock", fake.clock)
    oidc_service.reset_caches()
    yield fake
    oidc_service.reset_caches()
    formatter = JSONFormatter("auth")
    formatted = [formatter.format(record) for record in caplog.records]
    extra = fake.extra_secrets + ([fake.id_token] if fake.id_token else [])
    assert secret_findings(formatted, extra) == []


def assert_clean_error(error: Exception, idp: FakeIdP | None = None) -> None:
    texts = [str(error), repr(error), *(str(arg) for arg in error.args)]
    extra = (idp.extra_secrets + [idp.id_token or ""]) if idp is not None else []
    assert secret_findings(texts, extra) == []
    # `raise ... from None` sets __suppress_context__; without it an exception
    # raised while handling another carries that one as __context__.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__ is True


def failure_records(caplog, action: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "action", None) == action]


async def redeem() -> VerifiedIdToken:
    return await oidc_service.redeem_code(CODE, VERIFIER, NONCE)


async def expect_rejected(idp, caplog, reason: str, error_class: str | None = None):
    with pytest.raises(OidcRejectedError) as exc_info:
        await redeem()
    assert exc_info.value.reason == reason
    assert str(exc_info.value) == f"single sign-on refused ({reason})"
    assert_clean_error(exc_info.value, idp)
    records = failure_records(caplog, "oidc_rejected")
    assert [r.reason for r in records] == [reason]
    assert getattr(records[0], "error_class", None) == error_class
    return records[0]


async def expect_unavailable(call, idp, caplog, reason: str):
    with pytest.raises(OidcUnavailableError) as exc_info:
        await call()
    assert exc_info.value.reason == reason
    assert str(exc_info.value) == f"single sign-on unavailable ({reason})"
    assert_clean_error(exc_info.value, idp)
    assert [r.reason for r in failure_records(caplog, "oidc_unavailable")] == [reason]


def raises(error_class):
    def answer(request: httpx.Request) -> httpx.Response:
        raise error_class(IDP_BODY_TEXT, request=request)

    return answer


def answers(status: int, **kwargs):
    return lambda request: httpx.Response(status, **kwargs)


# --- the happy path ---


async def test_the_baseline_token_verifies(idp):
    idp.use_token()
    verified = await redeem()
    assert verified == VerifiedIdToken(
        issuer=ISSUER,
        subject=SUBJECT,
        email=EMAIL,
        email_verified=True,
        username_claim=USERNAME,
        name=FULL_NAME,
    )
    assert idp.counts == {"discovery": 1, "jwks": 1, "token": 1, "other": 0}


@pytest.mark.parametrize("algorithm", oidc_service._KEY_TYPES)
async def test_each_accepted_algorithm_verifies_with_a_key_of_its_type(
    algorithm, idp, keys, monkeypatch
):
    monkeypatch.setattr(settings, "oidc_id_token_algorithm", algorithm)
    signer = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}.get(algorithm, "rsa")
    # No `alg` member, so the pinned setting alone decides (an RSA JWK without
    # one would otherwise default to RS256 in PyJWT).
    idp.jwks = {"keys": [public_jwk(keys[signer], KID)]}
    idp.use_token(TokenSpec(algorithm=algorithm, signer=signer))
    assert (await redeem()).subject == SUBJECT


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": [CLIENT_ID]},
        {"aud": [CLIENT_ID, "other-client"], "azp": CLIENT_ID},
        {"azp": CLIENT_ID},
        {"exp": -5},
        {"iat": 5},
        {"nbf": 5},
        {"jwk_alg": "RS256", "jwk_use": "sig"},
    ],
    ids=[
        "aud-list-of-one",
        "aud-multi-with-azp",
        "azp-equal",
        "exp-inside-leeway",
        "iat-inside-leeway",
        "nbf-inside-leeway",
        "jwk-alg-and-use-matching",
    ],
)
async def test_tokens_inside_every_rule_verify(changes, idp, keys):
    spec = TokenSpec()
    now = int(datetime.now(UTC).timestamp())
    for name, value in changes.items():
        if name == "jwk_alg":
            idp.jwks["keys"][0]["alg"] = value
        elif name == "jwk_use":
            idp.jwks["keys"][0]["use"] = value
        elif name in ("exp", "iat", "nbf"):
            spec.claims[name] = now + value
        else:
            spec.claims[name] = value
    idp.use_token(spec)
    assert (await redeem()).subject == SUBJECT


# --- the refusal matrix: one rule per case, everything else valid ---


def _drop(name):
    def change(spec, idp, keys, monkeypatch):
        spec.claims.pop(name)

    return change


def _claim(name, value):
    def change(spec, idp, keys, monkeypatch):
        if value in ("PAST12", "FUTURE12"):
            now = int(datetime.now(UTC).timestamp())
            spec.claims[name] = now - 12 if value == "PAST12" else now + 12
        else:
            spec.claims[name] = value

    return change


def _spec(**attrs):
    def change(spec, idp, keys, monkeypatch):
        for name, value in attrs.items():
            setattr(spec, name, value)

    return change


def _hmac_with(secret_name: str, algorithm: str = "HS256"):
    def change(spec, idp, keys, monkeypatch):
        secret = {
            "client_secret": CLIENT_SECRET.encode(),
            "public_pem": keys["rsa"]
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ),
            "jwk_n": idp.jwks["keys"][0]["n"].encode(),
        }[secret_name]
        spec.algorithm = algorithm
        spec.hmac_secret = secret

    return change


def _jwk_member(name, value):
    def change(spec, idp, keys, monkeypatch):
        idp.jwks["keys"][0][name] = value

    return change


def _pinned_ps256(spec, idp, keys, monkeypatch):
    monkeypatch.setattr(settings, "oidc_id_token_algorithm", "PS256")


def _kid_names_an_ec_key(spec, idp, keys, monkeypatch):
    idp.jwks = {"keys": [public_jwk(keys["P-256"], KID)]}


def _es256_key_on_another_curve(spec, idp, keys, monkeypatch):
    monkeypatch.setattr(settings, "oidc_id_token_algorithm", "ES256")
    idp.jwks = {"keys": [public_jwk(keys["P-384"], KID)]}
    spec.algorithm = "ES256"
    spec.signer = "P-256"


def _two_keys_share_the_kid(spec, idp, keys, monkeypatch):
    idp.jwks["keys"].append(public_jwk(keys["rsa_rotated"], KID))


def _short_rsa_key(spec, idp, keys, monkeypatch):
    idp.jwks = {"keys": [public_jwk(keys["rsa_1024"], KID)]}
    spec.signer = "rsa_1024"


def _kid_held_by_two_unsuitable_keys(spec, idp, keys, monkeypatch):
    idp.jwks = {
        "keys": [
            public_jwk(keys["rsa"], KID, use="enc"),
            public_jwk(keys["P-256"], KID),
        ]
    }


def _raw_token(make):
    def change(spec, idp, keys, monkeypatch):
        spec.raw = make(keys)

    return change


# (change, reason, the PyJWT class logged for an id_token_invalid reason)
REFUSALS: dict[str, tuple[Any, str, str | None]] = {
    # Signature and key selection.
    "signature-by-another-key": (
        _spec(signer="attacker"),
        "id_token_invalid",
        "InvalidSignatureError",
    ),
    "payload-tampered": (_spec(tamper_sub=True), "id_token_invalid", "InvalidSignatureError"),
    "kid-unknown": (_spec(kid="SENTINEL-kid-unknown"), "unknown_kid", None),
    "kid-missing": (_spec(kid=None), "missing_kid", None),
    "kid-ambiguous": (_two_keys_share_the_kid, "ambiguous_kid", None),
    "jwk-use-enc": (_jwk_member("use", "enc"), "key_use_mismatch", None),
    "jwk-alg-other": (_jwk_member("alg", "RS512"), "key_algorithm_mismatch", None),
    "jwk-kty-ec-for-rs256": (_kid_names_an_ec_key, "key_type_mismatch", None),
    "jwk-curve-p384-for-es256": (_es256_key_on_another_curve, "key_type_mismatch", None),
    "token-malformed": (
        _spec(kid=KID, unsigned=False, hmac_secret=None, claims=None),
        "malformed_token",
        "DecodeError",
    ),
    # Algorithm pin and algorithm confusion.
    "alg-rs384-when-rs256-pinned": (_spec(algorithm="RS384"), "algorithm_mismatch", None),
    "alg-ps256-when-rs256-pinned": (_spec(algorithm="PS256"), "algorithm_mismatch", None),
    "alg-rs256-when-ps256-pinned": (_pinned_ps256, "algorithm_mismatch", None),
    "alg-none": (_spec(unsigned=True), "algorithm_mismatch", None),
    "alg-hs256-keyed-by-client-secret": (_hmac_with("client_secret"), "algorithm_mismatch", None),
    "alg-hs512-keyed-by-client-secret": (
        _hmac_with("client_secret", "HS512"),
        "algorithm_mismatch",
        None,
    ),
    "alg-hs256-keyed-by-public-key-pem": (_hmac_with("public_pem"), "algorithm_mismatch", None),
    "alg-hs256-keyed-by-jwk-modulus": (_hmac_with("jwk_n"), "algorithm_mismatch", None),
    # iss.
    "iss-different": (
        _claim("iss", "https://evil.test/realms/herd"),
        "id_token_invalid",
        "InvalidIssuerError",
    ),
    "iss-trailing-slash": (_claim("iss", ISSUER + "/"), "id_token_invalid", "InvalidIssuerError"),
    "iss-missing": (_drop("iss"), "id_token_invalid", "MissingRequiredClaimError"),
    # aud and azp.
    "aud-another-client": (
        _claim("aud", "other-client"),
        "id_token_invalid",
        "InvalidAudienceError",
    ),
    "aud-list-without-client": (
        _claim("aud", ["other-client", "third"]),
        "id_token_invalid",
        "InvalidAudienceError",
    ),
    "aud-missing": (_drop("aud"), "id_token_invalid", "MissingRequiredClaimError"),
    "azp-missing-with-two-audiences": (
        _claim("aud", [CLIENT_ID, "other-client"]),
        "azp_missing",
        None,
    ),
    "azp-another-client": (_claim("azp", "other-client"), "azp_mismatch", None),
    # Time.
    "exp-past-the-leeway": (_claim("exp", "PAST12"), "id_token_invalid", "ExpiredSignatureError"),
    "exp-missing": (_drop("exp"), "id_token_invalid", "MissingRequiredClaimError"),
    "iat-past-the-leeway": (
        _claim("iat", "FUTURE12"),
        "id_token_invalid",
        "ImmatureSignatureError",
    ),
    "iat-missing": (_drop("iat"), "id_token_invalid", "MissingRequiredClaimError"),
    "nbf-past-the-leeway": (
        _claim("nbf", "FUTURE12"),
        "id_token_invalid",
        "ImmatureSignatureError",
    ),
    # nonce.
    "nonce-different": (_claim("nonce", "SENTINEL-another-nonce"), "nonce_mismatch", None),
    "nonce-prefix-of-expected": (_claim("nonce", NONCE[:-1]), "nonce_mismatch", None),
    "nonce-not-a-string": (_claim("nonce", 12345), "nonce_mismatch", None),
    "nonce-missing": (_drop("nonce"), "id_token_invalid", "MissingRequiredClaimError"),
    # sub.
    "sub-missing": (_drop("sub"), "id_token_invalid", "MissingRequiredClaimError"),
    "sub-empty": (_claim("sub", ""), "subject_invalid", None),
    "sub-not-a-string": (_claim("sub", 42), "id_token_invalid", "InvalidSubjectError"),
    "sub-over-255-characters": (_claim("sub", "S" * 256), "subject_invalid", None),
    "sub-white-space-only": (_claim("sub", " \t "), "subject_invalid", None),
    "sub-with-nul": (_claim("sub", "SENTINEL-a\x00b"), "subject_invalid", None),
    "sub-with-unit-separator": (_claim("sub", "SENTINEL-a\x1fb"), "subject_invalid", None),
    "sub-with-del": (_claim("sub", "SENTINEL-a\x7fb"), "subject_invalid", None),
    "sub-with-lone-surrogate": (_claim("sub", "SENTINEL-a\ud800"), "subject_invalid", None),
    "nonce-lone-surrogate": (_claim("nonce", "\ud800"), "nonce_mismatch", None),
    # Access tokens (RFC 9068) are not ID tokens.
    "typ-at+jwt": (_spec(header_extra={"typ": "at+jwt"}), "access_token_type", None),
    "typ-application-at+jwt-upper-case": (
        _spec(header_extra={"typ": "application/AT+JWT"}),
        "access_token_type",
        None,
    ),
    # Key strength: PyJWT's minimum, enforced for ID tokens.
    "rsa-key-under-2048-bits": (_short_rsa_key, "key_too_short", "InvalidKeyError"),
    # Key selection filters by suitability first.
    "kid-held-only-by-unsuitable-keys": (
        _kid_held_by_two_unsuitable_keys,
        "no_suitable_key",
        None,
    ),
    # Token text PyJWT must never see, or must never get to raise past us.
    "token-not-ascii": (_raw_token(lambda keys: "SENTINEL-\u00e9.a.b"), "malformed_token", None),
    "token-lone-surrogate": (_raw_token(lambda keys: "\ud800.a.b"), "malformed_token", None),
    "token-with-space": (_raw_token(lambda keys: "SENTINEL a.b.c"), "malformed_token", None),
    "header-deeply-nested": (
        _raw_token(lambda keys: _b64(b"[" * 100_000 + b"]" * 100_000) + ".e30.c2ln"),
        "malformed_token",
        "DecodeError",
    ),
    "payload-deeply-nested-and-signed": (
        _raw_token(lambda keys: signed_raw(keys, b"[" * 100_000 + b"]" * 100_000)),
        "id_token_invalid",
        "DecodeError",
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
async def test_each_id_token_rule_refuses_a_token_otherwise_valid(
    name, idp, keys, monkeypatch, caplog
):
    change, reason, error_class = REFUSALS[name]
    spec = TokenSpec()
    change(spec, idp, keys, monkeypatch)
    if spec.claims is None:
        idp.id_token = "SENTINEL-not-a-jwt"
    else:
        idp.use_token(spec)
    await expect_rejected(idp, caplog, reason, error_class)


def test_the_refusal_matrix_covers_every_id_token_rule():
    """One case for each rule the module refuses with, so a rule added without a
    case, or a case left pointing at a removed reason, fails here."""
    covered = {reason for _, reason, _ in REFUSALS.values()}
    assert covered == {
        "id_token_invalid",
        "unknown_kid",
        "missing_kid",
        "ambiguous_kid",
        "key_use_mismatch",
        "key_algorithm_mismatch",
        "key_type_mismatch",
        "malformed_token",
        "algorithm_mismatch",
        "azp_missing",
        "azp_mismatch",
        "nonce_mismatch",
        "subject_invalid",
        "access_token_type",
        "key_too_short",
        "no_suitable_key",
    }
    pyjwt_classes = {cls for _, reason, cls in REFUSALS.values() if reason == "id_token_invalid"}
    assert pyjwt_classes == {
        "InvalidSignatureError",
        "InvalidIssuerError",
        "InvalidAudienceError",
        "MissingRequiredClaimError",
        "ExpiredSignatureError",
        "ImmatureSignatureError",
        "InvalidSubjectError",
        "DecodeError",
    }


async def test_a_jwk_pyjwt_cannot_load_for_the_pinned_algorithm_is_refused(idp, keys, caplog):
    # An RSA JWK with a damaged modulus: kty fits, PyJWK refuses to load it.
    good = public_jwk(keys["rsa"], KID)
    idp.jwks = {"keys": [dict(good, n="AA"), public_jwk(keys["rsa_rotated"], "other")]}
    idp.use_token()
    await expect_rejected(idp, caplog, "key_unusable", "InvalidKeyError")


# --- email_verified and the optional claims ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (None, None), ("true", False), ("false", False), (1, False)],
    ids=["true", "false", "null", "string-true", "string-false", "number"],
)
async def test_email_verified_maps_a_non_boolean_to_false(value, expected, idp):
    spec = TokenSpec()
    spec.claims["email_verified"] = value
    idp.use_token(spec)
    assert (await redeem()).email_verified is expected


async def test_absent_optional_claims_are_none(idp):
    spec = TokenSpec()
    for name in ("email", "email_verified", "preferred_username", "name"):
        spec.claims.pop(name)
    idp.use_token(spec)
    verified = await redeem()
    assert (verified.email, verified.email_verified, verified.username_claim, verified.name) == (
        None,
        None,
        None,
        None,
    )


async def test_non_string_or_empty_optional_claims_are_none(idp):
    spec = TokenSpec()
    spec.claims.update(email=["a@b.test"], preferred_username="", name={"given": "x"})
    idp.use_token(spec)
    verified = await redeem()
    assert (verified.email, verified.username_claim, verified.name) == (None, None, None)


async def test_the_configured_claim_names_are_read(idp, monkeypatch):
    monkeypatch.setattr(settings, "oidc_username_claim", "upn")
    monkeypatch.setattr(settings, "oidc_email_claim", "mail")
    spec = TokenSpec()
    spec.claims.update(upn="SENTINEL-upn", mail="sentinel-mail@example.test")
    idp.use_token(spec)
    verified = await redeem()
    assert (verified.username_claim, verified.email) == (
        "SENTINEL-upn",
        "sentinel-mail@example.test",
    )


# --- discovery fails closed and is never cached ---


def _discovery_change(**changes):
    def change(idp):
        for name, value in changes.items():
            if value is None:
                idp.discovery.pop(name, None)
            else:
                idp.discovery[name] = value

    return change


def _discovery_answer(answer):
    def change(idp):
        idp.overrides["discovery"] = answer

    return change


DISCOVERY_FAILURES = {
    "authorization-endpoint-query-names-state-blank": (
        _discovery_change(authorization_endpoint=AUTHORIZATION_ENDPOINT + "?state=&nonce"),
        "discovery_endpoint_invalid",
    ),
    "authorization-endpoint-bare-fragment": (
        _discovery_change(authorization_endpoint=AUTHORIZATION_ENDPOINT + "#"),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-userinfo": (
        _discovery_change(token_endpoint="https://u:p@idp.test/token"),
        "discovery_endpoint_invalid",
    ),
    "jwks-uri-non-ascii": (
        _discovery_change(jwks_uri="https://idp.test/c\u00e9rts"),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-with-space": (
        _discovery_change(token_endpoint="https://idp.test/to ken"),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-lone-surrogate": (
        _discovery_change(token_endpoint="https://idp.test/\ud800"),
        "discovery_endpoint_invalid",
    ),
    "status-200-deeply-nested": (
        _discovery_answer(answers(200, content=b"[" * 100_000 + b"]" * 100_000)),
        "discovery_not_json",
    ),
    "connect-error": (_discovery_answer(raises(httpx.ConnectError)), "discovery_transport"),
    "connect-timeout": (_discovery_answer(raises(httpx.ConnectTimeout)), "discovery_transport"),
    "read-timeout": (_discovery_answer(raises(httpx.ReadTimeout)), "discovery_transport"),
    "status-500": (_discovery_answer(answers(500, text=IDP_BODY_TEXT)), "discovery_status"),
    "status-404": (_discovery_answer(answers(404, text=IDP_BODY_TEXT)), "discovery_status"),
    "redirect-not-followed": (
        _discovery_answer(answers(302, headers={"Location": ISSUER + "/elsewhere"})),
        "discovery_status",
    ),
    "not-json": (_discovery_answer(answers(200, text=IDP_BODY_TEXT)), "discovery_not_json"),
    "json-array": (_discovery_answer(answers(200, json=[IDP_BODY_TEXT])), "discovery_invalid"),
    "issuer-different": (
        _discovery_change(issuer="https://evil.test/realms/herd"),
        "discovery_issuer_mismatch",
    ),
    "issuer-trailing-slash": (_discovery_change(issuer=ISSUER + "/"), "discovery_issuer_mismatch"),
    "issuer-missing": (_discovery_change(issuer=None), "discovery_issuer_mismatch"),
    "authorization-endpoint-missing": (
        _discovery_change(authorization_endpoint=None),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-missing": (
        _discovery_change(token_endpoint=None),
        "discovery_endpoint_invalid",
    ),
    "jwks-uri-missing": (_discovery_change(jwks_uri=None), "discovery_endpoint_invalid"),
    "authorization-endpoint-http": (
        _discovery_change(authorization_endpoint="http://idp.test/auth"),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-http": (
        _discovery_change(token_endpoint="http://idp.test/token"),
        "discovery_endpoint_invalid",
    ),
    "jwks-uri-http": (
        _discovery_change(jwks_uri="http://idp.test/certs"),
        "discovery_endpoint_invalid",
    ),
    "another-endpoint-http": (
        _discovery_change(userinfo_endpoint="http://idp.test/userinfo"),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-relative": (
        _discovery_change(token_endpoint="/token"),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-not-a-string": (
        _discovery_change(token_endpoint=443),
        "discovery_endpoint_invalid",
    ),
    "token-endpoint-other-scheme": (
        _discovery_change(token_endpoint="ftp://idp.test/token"),
        "discovery_endpoint_invalid",
    ),
    "authorization-endpoint-fragment": (
        _discovery_change(authorization_endpoint=AUTHORIZATION_ENDPOINT + "#frag"),
        "discovery_endpoint_invalid",
    ),
    "authorization-endpoint-query-names-state": (
        _discovery_change(authorization_endpoint=AUTHORIZATION_ENDPOINT + "?state=fixed"),
        "discovery_endpoint_invalid",
    ),
    "response-types-missing": (
        _discovery_change(response_types_supported=None),
        "discovery_code_flow_unsupported",
    ),
    "response-types-without-code": (
        _discovery_change(response_types_supported=["id_token", "code id_token"]),
        "discovery_code_flow_unsupported",
    ),
    "pkce-methods-without-s256": (
        _discovery_change(code_challenge_methods_supported=["plain"]),
        "discovery_s256_unsupported",
    ),
    "pkce-methods-a-string": (
        _discovery_change(code_challenge_methods_supported="S256"),
        "discovery_s256_unsupported",
    ),
    "pkce-methods-empty": (
        _discovery_change(code_challenge_methods_supported=[]),
        "discovery_s256_unsupported",
    ),
    "pkce-methods-null": (
        lambda idp: idp.discovery.update(code_challenge_methods_supported=None),
        "discovery_s256_unsupported",
    ),
}


@pytest.mark.parametrize("name", sorted(DISCOVERY_FAILURES))
async def test_each_discovery_failure_is_unavailable_and_not_cached(name, idp, caplog):
    change, reason = DISCOVERY_FAILURES[name]
    pristine = dict(idp.discovery)
    change(idp)
    await expect_unavailable(oidc_service.build_authorization_request, idp, caplog, reason)
    assert idp.counts["discovery"] == 1
    assert idp.counts["other"] == 0
    # The provider recovers; the very next call fetches again and succeeds.
    idp.overrides.clear()
    idp.discovery = pristine
    await oidc_service.build_authorization_request()
    assert idp.counts["discovery"] == 2


async def test_a_discovery_document_naming_no_pkce_methods_is_accepted(idp):
    idp.discovery.pop("code_challenge_methods_supported")
    request = await oidc_service.build_authorization_request()
    assert request.url.startswith(AUTHORIZATION_ENDPOINT + "?")


async def test_a_failed_discovery_stops_redeem_before_the_code_is_sent(idp, caplog):
    idp.overrides["discovery"] = answers(503)
    idp.use_token()
    await expect_unavailable(redeem, idp, caplog, "discovery_status")
    assert idp.counts["token"] == 0


async def test_discovery_url_drops_one_trailing_slash_and_compares_the_issuer_exactly(
    idp, monkeypatch
):
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER + "/")
    idp.discovery["issuer"] = ISSUER + "/"
    await oidc_service.build_authorization_request()
    assert [str(r.url) for r in idp.requests] == [DISCOVERY_URL]


async def test_plain_http_is_accepted_only_when_https_is_not_required(idp, monkeypatch, caplog):
    http_issuer = "http://oidc-test:5556/dex"
    monkeypatch.setattr(settings, "oidc_issuer", http_issuer)
    monkeypatch.setattr(settings, "oidc_redirect_uri", "http://herd.test/api/auth/oidc/callback")
    monkeypatch.setattr(settings, "oidc_require_https", False)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == http_issuer + "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": http_issuer,
                    "authorization_endpoint": http_issuer + "/auth",
                    "token_endpoint": http_issuer + "/token",
                    "jwks_uri": http_issuer + "/keys",
                    "response_types_supported": ["code"],
                },
            )
        if url == http_issuer + "/keys":
            return httpx.Response(200, json=idp.jwks)
        return httpx.Response(200, json={"id_token": idp.id_token})

    monkeypatch.setattr(oidc_service, "transport", httpx.MockTransport(handler))
    spec = TokenSpec()
    spec.claims["iss"] = http_issuer
    idp.use_token(spec)
    request = await oidc_service.build_authorization_request()
    assert request.url.startswith(http_issuer + "/auth?")
    assert (await redeem()).issuer == http_issuer


# --- JWKS fails closed and is never cached ---


def _jwks_answer(answer):
    def change(idp):
        idp.overrides["jwks"] = answer

    return change


def _jwks_document(document):
    def change(idp):
        idp.jwks = document

    return change


JWKS_FAILURES = {
    "connect-error": (_jwks_answer(raises(httpx.ConnectError)), "jwks_transport"),
    "read-timeout": (_jwks_answer(raises(httpx.ReadTimeout)), "jwks_transport"),
    "status-500": (_jwks_answer(answers(500, text=IDP_BODY_TEXT)), "jwks_status"),
    "redirect-not-followed": (
        _jwks_answer(answers(301, headers={"Location": ISSUER + "/elsewhere"})),
        "jwks_status",
    ),
    "not-json": (_jwks_answer(answers(200, text=IDP_BODY_TEXT)), "jwks_not_json"),
    "json-array": (_jwks_document([IDP_BODY_TEXT]), "jwks_invalid"),
    "keys-missing": (_jwks_document({"other": []}), "jwks_invalid"),
    "keys-not-a-list": (_jwks_document({"keys": IDP_BODY_TEXT}), "jwks_invalid"),
    "keys-empty": (_jwks_document({"keys": []}), "jwks_invalid"),
    "key-not-an-object": (_jwks_document({"keys": [IDP_BODY_TEXT]}), "jwks_invalid"),
    "no-usable-key": (
        _jwks_document({"keys": [{"kty": "SENTINEL-kty", "kid": KID}]}),
        "jwks_invalid",
    ),
}


@pytest.mark.parametrize("name", sorted(JWKS_FAILURES))
async def test_each_jwks_failure_is_unavailable_and_not_cached(name, idp, caplog):
    change, reason = JWKS_FAILURES[name]
    pristine = idp.jwks
    change(idp)
    idp.use_token()
    await expect_unavailable(redeem, idp, caplog, reason)
    assert idp.counts["jwks"] == 1
    idp.overrides.clear()
    idp.jwks = pristine
    assert (await redeem()).subject == SUBJECT
    assert idp.counts["jwks"] == 2


# --- the token endpoint ---


TOKEN_UNAVAILABLE = {
    "connect-error": (raises(httpx.ConnectError), "token_transport"),
    "read-timeout": (raises(httpx.ReadTimeout), "token_transport"),
    "status-500": (answers(500, text=IDP_BODY_TEXT), "token_status"),
    "status-502": (answers(502, text=IDP_BODY_TEXT), "token_status"),
    "status-503": (answers(503, json={"error": "temporarily_unavailable"}), "token_status"),
    "redirect-not-followed": (answers(302, headers={"Location": ISSUER + "/x"}), "token_status"),
    "not-json": (answers(200, text=IDP_BODY_TEXT), "token_not_json"),
}


@pytest.mark.parametrize("name", sorted(TOKEN_UNAVAILABLE))
async def test_each_token_endpoint_outage_is_unavailable(name, idp, caplog):
    answer, reason = TOKEN_UNAVAILABLE[name]
    idp.overrides["token"] = answer
    await expect_unavailable(redeem, idp, caplog, reason)
    assert idp.counts["other"] == 0


TOKEN_REFUSED = {
    "invalid-grant": (
        answers(400, json={"error": "invalid_grant", "error_description": IDP_ERROR_TEXT}),
        400,
        "invalid_grant",
    ),
    "invalid-client": (answers(401, json={"error": "invalid_client"}), 401, "invalid_client"),
    "unregistered-error-code": (
        answers(400, json={"error": "SENTINEL-custom", "error_description": IDP_ERROR_TEXT}),
        400,
        "other",
    ),
    "text-body": (answers(403, text=IDP_BODY_TEXT), 403, "other"),
}


@pytest.mark.parametrize("name", sorted(TOKEN_REFUSED))
async def test_each_token_endpoint_refusal_is_rejected_with_only_the_registered_code(
    name, idp, caplog
):
    answer, status, oauth_error = TOKEN_REFUSED[name]
    idp.overrides["token"] = answer
    record = await expect_rejected(idp, caplog, "token_refused")
    assert (record.status_code, record.oauth_error) == (status, oauth_error)


@pytest.mark.parametrize(
    "body",
    [{}, {"id_token": ""}, {"id_token": 5}, {"access_token": IDP_ACCESS_TOKEN}, [IDP_BODY_TEXT]],
    ids=["empty", "empty-string", "number", "access-token-only", "json-array"],
)
async def test_a_token_answer_without_an_id_token_is_rejected(body, idp, caplog):
    idp.overrides["token"] = answers(200, json=body)
    await expect_rejected(idp, caplog, "no_id_token")
    assert idp.counts["jwks"] == 0


async def test_the_token_request_is_the_adr_form_with_client_secret_basic(idp):
    idp.use_token()
    await redeem()
    [request] = [r for r in idp.requests if str(r.url) == TOKEN_ENDPOINT]
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.content.decode(), keep_blank_values=True) == {
        "grant_type": ["authorization_code"],
        "code": [CODE],
        "redirect_uri": [REDIRECT_URI],
        "code_verifier": [VERIFIER],
    }
    # RFC 6749 section 2.3.1: each half form-urlencoded before the Basic encoding.
    expected = "herd+client%2F1:SENTINEL-client-secret%3Awith%2Bodd%2Fchars%25"
    assert request.headers["authorization"] == (
        "Basic " + base64.b64encode(expected.encode()).decode()
    )
    others = [r for r in idp.requests if r is not request]
    assert [(r.method, str(r.url)) for r in others] == [("GET", DISCOVERY_URL), ("GET", JWKS_URI)]
    assert all("authorization" not in r.headers for r in others)


# --- caching and the unknown-kid refetch floor ---


async def test_discovery_and_jwks_are_cached_for_3600_seconds(idp):
    idp.use_token()
    await redeem()
    idp.clock.now += 3599.9
    await redeem()
    await oidc_service.build_authorization_request()
    assert (idp.counts["discovery"], idp.counts["jwks"]) == (1, 1)
    idp.clock.now += 0.1
    await redeem()
    assert (idp.counts["discovery"], idp.counts["jwks"]) == (2, 2)


async def test_an_unknown_kid_refetches_the_jwks_at_most_once_per_60_seconds(idp, keys, caplog):
    idp.use_token()
    await redeem()
    assert idp.counts["jwks"] == 1
    unknown = TokenSpec(kid="SENTINEL-kid-unknown")

    async def refused_at(offset: float) -> None:
        idp.clock.now = 1000.0 + offset
        idp.use_token(unknown)
        with pytest.raises(OidcRejectedError) as exc_info:
            await redeem()
        assert exc_info.value.reason == "unknown_kid"

    await refused_at(59.9)  # inside the floor of the first fetch: no refetch
    assert idp.counts["jwks"] == 1
    await refused_at(60.0)  # the floor has passed: one refetch, still unknown
    assert idp.counts["jwks"] == 2
    await refused_at(61.0)
    await refused_at(119.9)
    assert idp.counts["jwks"] == 2
    await refused_at(120.0)
    assert idp.counts["jwks"] == 3
    assert len(failure_records(caplog, "oidc_rejected")) == 5


async def test_a_rotated_signing_key_is_picked_up_by_the_refetch(idp, keys):
    idp.use_token()
    await redeem()
    idp.jwks = {"keys": [public_jwk(keys["rsa_rotated"], "SENTINEL-kid-2")]}
    idp.extra_secrets.append("SENTINEL-kid-2")
    idp.clock.now += 61
    idp.use_token(TokenSpec(kid="SENTINEL-kid-2", signer="rsa_rotated"))
    assert (await redeem()).subject == SUBJECT
    assert idp.counts["jwks"] == 2


async def test_concurrent_unknown_kids_share_one_refetch(idp):
    idp.use_token()
    await redeem()
    idp.clock.now += 120
    idp.use_token(TokenSpec(kid="SENTINEL-kid-unknown"))
    results = await asyncio.gather(*(redeem() for _ in range(8)), return_exceptions=True)
    assert all(isinstance(r, OidcRejectedError) and r.reason == "unknown_kid" for r in results)
    assert idp.counts["jwks"] == 2


async def test_a_failed_refetch_is_not_cached_and_still_consumes_the_floor(idp, caplog):
    idp.use_token()
    await redeem()
    idp.clock.now += 61
    idp.overrides["jwks"] = answers(500)
    idp.use_token(TokenSpec(kid="SENTINEL-kid-unknown"))
    with pytest.raises(OidcUnavailableError):
        await redeem()
    assert idp.counts["jwks"] == 2
    # The failure emptied the cache instead of filling it: the next token, a
    # known kid, fetches again (no floor applies to an empty cache).
    idp.overrides.clear()
    idp.use_token()
    assert (await redeem()).subject == SUBJECT
    assert idp.counts["jwks"] == 3


# --- the authorization request ---


async def test_the_authorization_url_carries_exactly_the_adr_parameters(idp):
    request = await oidc_service.build_authorization_request()
    parts = urlsplit(request.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_ENDPOINT
    assert parts.fragment == ""
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    assert sorted(name for name, _ in pairs) == sorted(
        [
            "response_type",
            "client_id",
            "redirect_uri",
            "scope",
            "state",
            "nonce",
            "code_challenge",
            "code_challenge_method",
            "response_mode",
        ]
    )
    query = dict(pairs)
    assert query == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid email profile",
        "state": request.state,
        "nonce": request.nonce,
        "code_challenge": query["code_challenge"],
        "code_challenge_method": "S256",
        "response_mode": "query",
    }
    digest = hashlib.sha256(request.code_verifier.encode("ascii")).digest()
    assert query["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert "=" not in query["code_challenge"]
    assert request.code_verifier not in request.url


async def test_state_nonce_and_verifier_have_the_adr_sizes(idp):
    request = await oidc_service.build_authorization_request()
    for value in (request.state, request.nonce):
        assert len(value) == 43
        assert len(base64.urlsafe_b64decode(value + "=")) == 32
    unreserved = set(string.ascii_letters + string.digits + "-._~")
    assert len(request.code_verifier) == 64
    assert set(request.code_verifier) <= unreserved


async def test_state_nonce_and_verifier_come_from_secrets(idp, monkeypatch):
    calls: list = []
    real_token_urlsafe = oidc_service.secrets.token_urlsafe
    real_choice = oidc_service.secrets.choice

    def token_urlsafe(nbytes):
        calls.append(("token_urlsafe", nbytes))
        return real_token_urlsafe(nbytes)

    def choice(alphabet):
        calls.append(("choice", alphabet))
        return real_choice(alphabet)

    monkeypatch.setattr(oidc_service.secrets, "token_urlsafe", token_urlsafe)
    monkeypatch.setattr(oidc_service.secrets, "choice", choice)
    await oidc_service.build_authorization_request()
    unreserved = string.ascii_letters + string.digits + "-._~"
    assert calls == [("token_urlsafe", 32), ("token_urlsafe", 32)] + [("choice", unreserved)] * 64


async def test_every_authorization_request_is_fresh(idp):
    requests = [await oidc_service.build_authorization_request() for _ in range(50)]
    for name in ("state", "nonce", "code_verifier"):
        assert len({getattr(r, name) for r in requests}) == 50
    assert idp.counts["discovery"] == 1


async def test_an_authorization_endpoint_query_is_kept_before_the_adr_parameters(idp):
    idp.discovery["authorization_endpoint"] = AUTHORIZATION_ENDPOINT + "?p=b2c_1_signin"
    request = await oidc_service.build_authorization_request()
    pairs = parse_qsl(urlsplit(request.url).query)
    assert pairs[0] == ("p", "b2c_1_signin")
    assert [name for name, _ in pairs[1:]] == list(oidc_service.AUTHORIZATION_PARAMETERS)


def test_the_authorization_request_repr_shows_no_secret():
    request = AuthorizationRequest(
        url="u-SENTINEL", state="s-SENTINEL", nonce="n-SENTINEL", code_verifier="v-SENTINEL"
    )
    assert "SENTINEL" not in repr(request)


def test_the_dataclasses_are_frozen():
    request = AuthorizationRequest(url="u", state="s", nonce="n", code_verifier="v")
    verified = VerifiedIdToken("i", "s", None, None, None, None)
    for obj, name in ((request, "state"), (verified, "subject")):
        with pytest.raises(AttributeError):
            setattr(obj, name, "x")


# --- dark by construction, and incomplete settings ---


@pytest.mark.parametrize("mode", ["local", "ldap"])
async def test_nothing_is_fetched_outside_oidc_mode(mode, idp, monkeypatch, caplog):
    monkeypatch.setattr(settings, "auth_method", mode)
    await expect_unavailable(oidc_service.build_authorization_request, idp, caplog, "not_enabled")
    caplog.clear()
    await expect_unavailable(redeem, idp, caplog, "not_enabled")
    assert idp.requests == []


@pytest.mark.parametrize(
    ("name", "value", "variable"),
    [
        ("oidc_issuer", "", "OIDC_ISSUER"),
        ("oidc_client_id", " ", "OIDC_CLIENT_ID"),
        ("oidc_client_secret", "", "OIDC_CLIENT_SECRET"),
        ("oidc_redirect_uri", "", "OIDC_REDIRECT_URI"),
        ("oidc_redirect_uri", "https://herd.test/api/auth/oidc/other", "OIDC_REDIRECT_URI"),
        ("oidc_scopes", "email profile", "OIDC_SCOPES"),
        ("oidc_email_claim", "", "OIDC_EMAIL_CLAIM"),
        ("oidc_ca_cert", "/nonexistent/SENTINEL-ca.pem", "OIDC_CA_CERT"),
    ],
)
async def test_incomplete_settings_are_unavailable_before_any_request(
    name, value, variable, idp, monkeypatch, caplog
):
    monkeypatch.setattr(settings, name, value)
    await expect_unavailable(
        oidc_service.build_authorization_request, idp, caplog, "settings_incomplete"
    )
    [record] = failure_records(caplog, "oidc_unavailable")
    assert record.settings == [variable]
    caplog.clear()
    await expect_unavailable(redeem, idp, caplog, "settings_incomplete")
    assert idp.requests == []


@pytest.mark.parametrize(
    "args",
    [("", VERIFIER, NONCE), (CODE, "", NONCE), (CODE, VERIFIER, "")],
    ids=["code", "verifier", "nonce"],
)
async def test_an_empty_input_is_rejected_before_any_request(args, idp, caplog):
    with pytest.raises(OidcRejectedError) as exc_info:
        await oidc_service.redeem_code(*args)
    assert exc_info.value.reason == "missing_input"
    assert_clean_error(exc_info.value, idp)
    assert idp.requests == []


# --- the HTTP client ---


async def test_the_http_client_has_the_adr_timeouts_and_follows_no_redirect(idp):
    async with oidc_service._client() as client:
        assert client.timeout == httpx.Timeout(10.0, connect=5.0)
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 10.0
        assert client.follow_redirects is False
    assert oidc_service.HTTP_TIMEOUT == httpx.Timeout(10.0, connect=5.0)


def _write_ca(path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "herd test ca")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_tls_uses_the_system_store_without_a_ca_bundle(idp):
    assert oidc_service._tls_verify() is True


def test_tls_trusts_the_configured_ca_bundle(idp, monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    _write_ca(ca)
    monkeypatch.setattr(settings, "oidc_ca_cert", str(ca))
    context = oidc_service._tls_verify()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert [c["subject"] for c in context.get_ca_certs()] == [((("commonName", "herd test ca"),),)]


def test_an_unreadable_ca_bundle_is_unavailable(idp, monkeypatch, tmp_path, caplog):
    ca = tmp_path / "ca.pem"
    ca.write_text("SENTINEL not a certificate")
    monkeypatch.setattr(settings, "oidc_ca_cert", str(ca))
    with pytest.raises(OidcUnavailableError) as exc_info:
        oidc_service._tls_verify()
    assert exc_info.value.reason == "ca_cert_unusable"
    assert_clean_error(exc_info.value, idp)
    [record] = failure_records(caplog, "oidc_unavailable")
    assert record.error_class == "SSLError"


# --- log hygiene, the checker itself ---


async def test_the_success_path_logs_no_secret_and_the_check_sees_its_records(idp, caplog):
    idp.use_token()
    await oidc_service.build_authorization_request()
    await redeem()
    formatted = [JSONFormatter("auth").format(r) for r in caplog.records]
    # httpx logs one line per request, so the check below reads real records.
    assert len(formatted) >= 3
    assert secret_findings(formatted, [idp.id_token]) == []


def test_the_secret_check_finds_a_planted_secret():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "code %s", (CODE,), None)
    formatted = JSONFormatter("auth").format(record)
    assert secret_findings([formatted]) != []
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "m", (), None)
    record.reason = NONCE
    assert secret_findings([JSONFormatter("auth").format(record)]) != []


# --- key selection filters by suitability first ---


@pytest.mark.parametrize(
    "neighbour",
    ["rsa-enc", "ec", "rsa-other-alg"],
)
async def test_a_kid_shared_with_an_unsuitable_key_selects_the_suitable_one(neighbour, idp, keys):
    other = {
        "rsa-enc": public_jwk(keys["rsa_rotated"], KID, use="enc"),
        "ec": public_jwk(keys["P-256"], KID),
        "rsa-other-alg": public_jwk(keys["rsa_rotated"], KID, alg="RS512"),
    }[neighbour]
    idp.jwks = {"keys": [other, public_jwk(keys["rsa"], KID)]}
    idp.use_token()
    assert (await redeem()).subject == SUBJECT


# --- optional claims holding a control character or lone surrogate ---


@pytest.mark.parametrize(
    "value",
    ["SENTINEL-a\x00b", "SENTINEL-a\nb", "SENTINEL-a\x07b", "SENTINEL-a\x9fb", "SENTINEL-a\ud800"],
    ids=["nul", "newline", "bell", "c1-control", "lone-surrogate"],
)
@pytest.mark.parametrize("claim", ["email", "preferred_username", "name"])
async def test_an_optional_claim_with_a_control_character_is_none(claim, value, idp):
    spec = TokenSpec()
    spec.claims[claim] = value
    idp.use_token(spec)
    verified = await redeem()
    field_name = {"email": "email", "preferred_username": "username_claim", "name": "name"}[claim]
    assert getattr(verified, field_name) is None
    assert verified.subject == SUBJECT


# --- the overall request deadline ---


def _never_answers():
    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return answer


def _drips_then_stalls():
    async def body():
        yield b'{"issuer": '
        await asyncio.Event().wait()

    async def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers={"content-type": "application/json"})

    return answer


def test_the_overall_deadline_is_ten_seconds_beside_the_httpx_timeouts():
    assert oidc_service.HTTP_DEADLINE_SECONDS == 10.0
    assert oidc_service.HTTP_TIMEOUT == httpx.Timeout(10.0, connect=5.0)


@pytest.mark.parametrize("stall", ["no-answer", "body-stalls-after-a-byte"])
@pytest.mark.parametrize(
    ("endpoint", "reason"),
    [("discovery", "discovery_deadline"), ("jwks", "jwks_deadline"), ("token", "token_deadline")],
)
async def test_a_request_that_never_completes_is_cut_at_the_deadline(
    endpoint, reason, stall, idp, monkeypatch, caplog
):
    # The deadline is shortened for the test; its value is pinned above.
    monkeypatch.setattr(oidc_service, "HTTP_DEADLINE_SECONDS", 0.05)
    idp.overrides[endpoint] = _never_answers() if stall == "no-answer" else _drips_then_stalls()
    idp.use_token()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await expect_unavailable(redeem, idp, caplog, reason)
    assert loop.time() - started < 2.0
    # Not cached: the provider recovers and the next call succeeds.
    idp.overrides.clear()
    assert (await redeem()).subject == SUBJECT


# --- the refetch floor while a fetch is in flight ---


async def test_misses_arriving_during_an_in_flight_fetch_older_than_the_floor_refetch_once(idp):
    """A JWKS fetch starts (and stamps the floor) at t=1000 and blocks; two
    misses for an unknown kid arrive at t=1061, past the floor, and queue on the
    cache lock behind it. When it completes, exactly one refetch follows."""
    gate = asyncio.Event()

    async def gated(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return json_response(idp.jwks)

    idp.overrides["jwks"] = gated
    idp.use_token(TokenSpec(kid="SENTINEL-kid-unknown"))
    first = asyncio.create_task(redeem())
    while idp.counts["jwks"] < 1:
        await asyncio.sleep(0)
    idp.clock.now += 61
    later = [asyncio.create_task(redeem()) for _ in range(2)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert idp.counts["jwks"] == 1
    gate.set()
    results = await asyncio.gather(first, *later, return_exceptions=True)
    assert all(isinstance(r, OidcRejectedError) and r.reason == "unknown_kid" for r in results)
    assert idp.counts["jwks"] == 2


# --- hostile shapes: only the two error classes ever leave an entry point ---
#
# Every endpoint gets every body shape, and every field the code reads gets
# every hostile JSON value, spliced in as raw JSON bytes so shapes json.dumps
# cannot produce (depth, numbers past the digit limit) reach the code. The
# outcome must be the one the documented rule names: unavailable when the
# provider could not be read (no usable JSON, a document that fails
# validation, a 5xx or other non-200, non-4xx status), rejected when it
# answered and was refused (a 4xx, an answer or token that breaks a rule), and
# never any other exception.

DEEP = b"[" * 100_000 + b"]" * 100_000

# name: (raw bytes, whether json.loads accepts it)
BODY_SHAPES: dict[str, tuple[bytes, bool]] = {
    "empty": (b"", False),
    "not-json": (b"SENTINEL-not-json", False),
    "non-utf8": (b"\xff\xfe\xfd", False),
    # json.loads decodes bytes with "surrogatepass": this parses to a string.
    "utf8-encoded-surrogate": (b'"\xed\xa0\x80"', True),
    "deeply-nested-array": (DEEP, False),
    "deeply-nested-object": (b'{"a":' * 50_000 + b"1" + b"}" * 50_000, False),
    "integer-past-the-digit-limit": (b"1" * 5000, False),
    "object-with-an-integer-past-the-limit": (b'{"x": ' + b"1" * 5000 + b"}", False),
    "json-null": (b"null", True),
    "json-true": (b"true", True),
    "json-number": (b"7", True),
    "json-string": (b'"SENTINEL-scalar"', True),
    "json-lone-surrogate-string": (b'"\\ud800"', True),
    "json-nan": (b"NaN", True),
    "json-array": (b"[]", True),
    "json-array-of-objects": (b"[{}]", True),
    "json-empty-object": (b"{}", True),
}

# name: (raw JSON value, whether json.loads accepts a document holding it)
VALUE_SHAPES: dict[str, tuple[bytes, bool]] = {
    "null": (b"null", True),
    "true": (b"true", True),
    "false": (b"false", True),
    "number": (b"7", True),
    "negative": (b"-1", True),
    "float": (b"1.5", True),
    "float-overflow": (b"1e999", True),
    "huge-integer": (b"9" * 400, True),
    "integer-past-the-digit-limit": (b"9" * 5000, False),
    "empty-list": (b"[]", True),
    "list-of-strings": (b'["SENTINEL-x"]', True),
    "list-of-objects": (b"[{}]", True),
    "empty-object": (b"{}", True),
    "nested-object": (b'{"a": {"b": []}}', True),
    "empty-string": (b'""', True),
    "lone-surrogate": (b'"\\ud800"', True),
    "control-character": (b'"SENTINEL-a\\u0000b"', True),
    "non-ascii": (b'"SENTINEL-\\u00e9t\\u00e9"', True),
    "deeply-nested": (DEEP, False),
}

PLACEHOLDER = "SENTINEL-PLACEHOLDER-VALUE"


def splice(document: dict, field_name: str, raw_value: bytes) -> bytes:
    """`document` as JSON bytes with `field_name` set to the raw JSON value."""
    placed = dict(document, **{field_name: PLACEHOLDER})
    return json.dumps(placed).encode().replace(json.dumps(PLACEHOLDER).encode(), raw_value)


def raw_answer(body: bytes, status: int = 200):
    return lambda request: httpx.Response(
        status, content=body, headers={"content-type": "application/json"}
    )


async def outcome_of(call, idp) -> str:
    """ "ok", "unavailable", or "rejected"; any other exception fails the test."""
    try:
        await call()
    except OidcUnavailableError as exc:
        assert_clean_error(exc, idp)
        return "unavailable"
    except OidcRejectedError as exc:
        assert_clean_error(exc, idp)
        return "rejected"
    return "ok"


UNAVAILABLE = frozenset({"unavailable"})
REJECTED = frozenset({"rejected"})
OK = frozenset({"ok"})
OK_OR_REJECTED = frozenset({"ok", "rejected"})
UNAVAILABLE_OR_REJECTED = frozenset({"unavailable", "rejected"})

DISCOVERY_FIELDS = (
    "issuer",
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "userinfo_endpoint",
    "response_types_supported",
    "code_challenge_methods_supported",
)
JWK_FIELDS = ("kid", "kty", "alg", "use", "n", "e")
CLAIM_FIELDS = (
    "iss",
    "aud",
    "sub",
    "nonce",
    "azp",
    "exp",
    "iat",
    "nbf",
    "email",
    "email_verified",
    "preferred_username",
    "name",
)
HEADER_FIELDS = ("kid", "alg", "typ")
STATUS_CODES = (100, 199, 201, 204, 299, 301, 399, 400, 401, 404, 499, 500, 502, 599, 999)


def _claim_expectation(field_name: str, value_name: str, parses: bool) -> frozenset:
    if not parses:
        return REJECTED  # the signed payload is not JSON: DecodeError
    if field_name in ("iss", "aud", "nonce"):
        return REJECTED  # no hostile value equals the expected one
    if field_name == "sub":
        return OK if value_name == "non-ascii" else REJECTED
    if field_name == "azp":
        return OK if value_name == "null" else REJECTED
    if field_name in ("exp", "iat", "nbf"):
        return OK_OR_REJECTED  # a huge exp, a past iat, or a past nbf is valid
    return OK  # optional claims map a hostile value to None (or False)


def _hostile_cases() -> dict[str, tuple]:
    cases: dict[str, tuple] = {}
    for name, (body, _) in BODY_SHAPES.items():
        cases[f"discovery-body-{name}"] = ("discovery-body", body, None, UNAVAILABLE)
        cases[f"jwks-body-{name}"] = ("jwks-body", body, None, UNAVAILABLE)
    for name, (body, parses) in BODY_SHAPES.items():
        cases[f"token-200-body-{name}"] = (
            "token-body",
            body,
            200,
            REJECTED if parses else UNAVAILABLE,
        )
        cases[f"token-400-body-{name}"] = ("token-body", body, 400, REJECTED)
        cases[f"token-503-body-{name}"] = ("token-body", body, 503, UNAVAILABLE)
        cases[f"id-token-header-{name}"] = ("header-bytes", body, None, REJECTED)
        cases[f"id-token-payload-{name}"] = ("payload-bytes", body, None, REJECTED)
    for status in STATUS_CODES:
        cases[f"token-status-{status}"] = (
            "token-status",
            b"",
            status,
            REJECTED if 400 <= status < 500 else UNAVAILABLE,
        )
    for value_name, (raw, parses) in VALUE_SHAPES.items():
        for field_name in DISCOVERY_FIELDS:
            cases[f"discovery-{field_name}-{value_name}"] = (
                "discovery-field",
                raw,
                field_name,
                UNAVAILABLE,
            )
        cases[f"jwks-keys-{value_name}"] = ("jwks-keys", raw, None, UNAVAILABLE)
        for field_name in JWK_FIELDS:
            for beside in ("alone", "beside-a-valid-key"):
                cases[f"jwk-{field_name}-{value_name}-{beside}"] = (
                    f"jwk-field-{beside}",
                    raw,
                    field_name,
                    UNAVAILABLE_OR_REJECTED if parses else UNAVAILABLE,
                )
        cases[f"token-id-token-{value_name}"] = (
            "token-field",
            raw,
            "id_token",
            REJECTED if parses else UNAVAILABLE,
        )
        for field_name in ("error", "error_description"):
            cases[f"token-400-{field_name}-{value_name}"] = (
                "token-error-field",
                raw,
                field_name,
                REJECTED,
            )
        for field_name in CLAIM_FIELDS:
            cases[f"claim-{field_name}-{value_name}"] = (
                "claim",
                raw,
                field_name,
                _claim_expectation(field_name, value_name, parses),
            )
        for field_name in HEADER_FIELDS:
            cases[f"header-{field_name}-{value_name}"] = (
                "header-field",
                raw,
                field_name,
                OK_OR_REJECTED if field_name == "typ" else REJECTED,
            )
    return cases


HOSTILE_CASES = _hostile_cases()


def test_the_hostile_shape_table_size():
    # 17 body shapes, 19 value shapes, 15 statuses; recomputed so a shape or a
    # field added to a table cannot silently drop out of the sweep.
    bodies, values = len(BODY_SHAPES), len(VALUE_SHAPES)
    per_value = len(DISCOVERY_FIELDS) + 1 + 2 * len(JWK_FIELDS) + 1 + 2
    per_value += len(CLAIM_FIELDS) + len(HEADER_FIELDS)
    expected = 2 * bodies + 5 * bodies + len(STATUS_CODES) + values * per_value
    assert len(HOSTILE_CASES) == expected == 856


@pytest.mark.parametrize("name", sorted(HOSTILE_CASES))
async def test_a_hostile_shape_yields_only_the_two_error_classes(name, idp, keys):
    kind, raw, detail, expected = HOSTILE_CASES[name]
    header = {"alg": "RS256", "typ": "JWT", "kid": KID}
    if kind == "discovery-body":
        idp.overrides["discovery"] = raw_answer(raw)
    elif kind == "discovery-field":
        idp.overrides["discovery"] = raw_answer(splice(idp.discovery, detail, raw))
    elif kind == "jwks-body":
        idp.overrides["jwks"] = raw_answer(raw)
    elif kind == "jwks-keys":
        idp.overrides["jwks"] = raw_answer(splice({}, "keys", raw))
    elif kind.startswith("jwk-field-"):
        key = json.loads(splice(public_jwk(keys["rsa"], KID), detail, b'"SENTINEL-slot"'))
        others = (
            [json.dumps(public_jwk(keys["rsa_rotated"], "SENTINEL-other")).encode()]
            if kind.endswith("beside-a-valid-key")
            else []
        )
        hostile = splice(public_jwk(keys["rsa"], KID), detail, raw)
        assert key[detail] == "SENTINEL-slot"
        idp.overrides["jwks"] = raw_answer(b'{"keys": [' + b", ".join([hostile, *others]) + b"]}")
    elif kind == "token-body":
        idp.overrides["token"] = raw_answer(raw, detail)
    elif kind == "token-status":
        idp.overrides["token"] = raw_answer(raw, detail)
    elif kind == "token-field":
        idp.overrides["token"] = raw_answer(splice({"access_token": "x"}, detail, raw))
    elif kind == "token-error-field":
        idp.overrides["token"] = raw_answer(splice({"error": "invalid_grant"}, detail, raw), 400)
    elif kind == "claim":
        idp.id_token = signed_raw(keys, splice(base_claims(), detail, raw))
    elif kind == "header-field":
        idp.id_token = signed_raw(
            keys, json.dumps(base_claims()).encode(), splice(header, detail, raw)
        )
    elif kind == "header-bytes":
        idp.id_token = signed_raw(keys, json.dumps(base_claims()).encode(), raw)
    elif kind == "payload-bytes":
        idp.id_token = signed_raw(keys, raw)
    else:
        raise AssertionError(kind)
    if idp.id_token is None:
        idp.use_token()
    assert await outcome_of(redeem, idp) in expected
