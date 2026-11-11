"""OpenID Connect relying-party protocol: discovery, JWKS, the authorization URL,
the code exchange, and ID-token verification (ADR 0016 phase 1, issue #37).

Dark by construction: every entry point refuses with `OidcUnavailableError`
unless `settings.auth_method == "oidc"`, which `Settings` does not admit until
ADR 0016 phase 6; tests reach the code by setting the attribute directly. No
route calls this module yet (phase 3 adds them).

Two entry points, the phase 1 half of "Internal interfaces between phases":

- `build_authorization_request()` returns an `AuthorizationRequest`: the IdP
  authorization URL (`response_type=code`, `client_id`, `redirect_uri`,
  `scope`, `state`, `nonce`, `code_challenge`, `code_challenge_method=S256`,
  `response_mode=query`, and nothing else HERD adds), with `state` and `nonce`
  32 random bytes each, base64url, and a 64-character PKCE verifier from the
  RFC 7636 unreserved set, all from `secrets`.
- `redeem_code(code, code_verifier, nonce)` posts the code to the token
  endpoint (form-encoded, `client_secret_basic` with both halves
  form-urlencoded), reads only `id_token` from the answer, verifies it, and
  returns a `VerifiedIdToken`.

Two errors, and only these two leave either entry point for any input the
provider shapes (`tests/test_oidc_service.py`, the hostile-shape table), neither
carrying IdP text in its `str()`: `OidcUnavailableError` (the provider could not
be asked or answered unusably: settings incomplete, transport error, timeout or
the overall deadline, non-200, a body that is not usable JSON, a discovery or
JWKS document that fails validation, a token endpoint 5xx or other non-200,
non-4xx status) and `OidcRejectedError` (the provider answered and the answer is
refused: a token endpoint 4xx, a JSON answer with no usable `id_token`, any
ID-token rule). Each carries a fixed `reason` from this module. Every one raised
while another exception is being handled is raised `from None`, so no upstream
exception rides along as its cause or context.

HTTP: one `httpx.AsyncClient` per request, `httpx.Timeout(10.0, connect=5.0)`
(5 s to connect, 10 s for each read, write, or pool wait), no redirect followed,
TLS verified (`OIDC_CA_CERT` as the bundle when set), and an overall deadline of
`HTTP_DEADLINE_SECONDS` (10 s) around each whole request, body included, since
httpx's timeouts bound each operation and a server that drips bytes never trips
them. Callers queued behind the cache lock wait for the request in flight, so
one caller can wait longer than 10 s in total.

ID-token rules, every one required: the token is printable ASCII; its header
names a `kid` and an `alg` equal to `OIDC_ID_TOKEN_ALGORITHM`, and its `typ`,
when present, is not an RFC 9068 access-token type (`at+jwt`); the JWKS keys
that can verify the pinned algorithm (`kty`, for EC `crv`, `use` absent or
`sig`, `alg` absent or the pinned one) are selected FIRST, and exactly one of
them has the `kid`; the signature verifies under
`herd_common.jwt_codec.decode_id_token` (exact `iss`, `aud` containing the
client id, `exp` and `iat` required, `exp`, `iat`, and `nbf` with the 60 s
ID-token leeway, an RSA key of at least 2048 bits); when `aud` holds more than one value
`azp` is required, and when `azp` is present it equals the client id; `nonce`
equals the flow's nonce (constant-time compare); `sub` is a string of at most
255 characters that is not empty or white space only and holds no control
character or lone surrogate. An optional claim (`email`, the username claim,
`name`) that is not a non-empty string, or holds a control character or lone
surrogate, is returned as None. `at_hash` is not checked, by decision (ADR 0016
D13).

Discovery (`{OIDC_ISSUER}/.well-known/openid-configuration`, one trailing slash
of the issuer dropped for the URL only) must be a JSON object whose `issuer`
equals `OIDC_ISSUER` exactly, whose `authorization_endpoint`, `token_endpoint`,
and `jwks_uri` are absolute printable-ASCII URLs with a host and no userinfo,
whose every `*_endpoint` value and `jwks_uri` is such a URL and https unless
`OIDC_REQUIRE_HTTPS` is false, whose `response_types_supported` contains `code`,
and whose `code_challenge_methods_supported`, when present, contains `S256`; the
authorization endpoint may carry its own query (RFC 6749 section 3.1) but no
fragment and none of the parameter names HERD sends, even with a blank value.
Discovery and JWKS are each cached for 3600 s in a `SingletonTTLCache`; a
failed fetch is returned as `Uncached` and never stored. A `kid` that no key in
the cached JWKS has refetches it only when no JWKS fetch was attempted in the
last 60 s, so a token with a random `kid` cannot drive more than one IdP fetch
a minute; the token is refused when the `kid` is still unknown.

Seams, module attributes looked up at call time (the `resolver` pattern of the
integration service's `destination.py`): `transport` (None in production;
tests set an `httpx.MockTransport`) and `clock` (`time.monotonic`). Tests call
`reset_caches()` between cases.

Log rule (ADR 0016 D9), for the log lines this module writes: never logged, in
a message or in `extra`: the client secret, the authorization code, `state`,
`nonce`, the PKCE verifier, the ID token, and any text the provider sent (an
`error_description`, a response body, a claim, a `kid`). A failure logs one
WARNING with a fixed message and `extra` holding `action`, the fixed `reason`,
and where they apply the exception class name, the HTTP status, and the RFC
6749 section 5.2 error code when it is one of the six registered values
(anything else is logged as `other`). This is stricter than ADR 0016 D9, which
allows truncated upstream text in the message; the stricter reading is
deliberate, since a provider's error text can echo the code it refused.
Third-party loggers are outside that rule: the `httpx` logger writes one INFO
line per request with the method, the URL (for discovery-supplied endpoints,
provider text), and the status; the `httpcore` logger writes the response
headers at DEBUG, which `herd_common.logging.setup_logging` keeps out by
setting `httpcore` to INFO.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import ssl
import string
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import httpx
import jwt
from herd_common.jwt_codec import decode_id_token
from herd_common.ttl_cache import SingletonTTLCache, Uncached

from app.config import oidc_configuration_problems, settings

logger = logging.getLogger(__name__)

# Fixed in code by ADR 0016 (Settings section), not settings.
DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600
JWKS_REFETCH_FLOOR_SECONDS = 60
# httpx's timeouts bound each operation (connect, each read, each write), not
# the request: a server that sends a byte every few seconds never trips a read
# timeout. HTTP_DEADLINE_SECONDS bounds each whole request, body included.
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
HTTP_DEADLINE_SECONDS = 10.0
STATE_BYTES = 32
NONCE_BYTES = 32
CODE_VERIFIER_LENGTH = 64
# RFC 7636 section 4.1: ALPHA / DIGIT / "-" / "." / "_" / "~".
CODE_VERIFIER_ALPHABET = string.ascii_letters + string.digits + "-._~"
MAX_SUBJECT_LENGTH = 255
DISCOVERY_PATH = "/.well-known/openid-configuration"

# The query parameters HERD puts on the authorization URL, in order.
AUTHORIZATION_PARAMETERS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "nonce",
    "code_challenge",
    "code_challenge_method",
    "response_mode",
)

# The JWK `kty` (and for EC the `crv`) each accepted algorithm needs.
_KEY_TYPES: dict[str, tuple[str, str | None]] = {
    "RS256": ("RSA", None),
    "RS384": ("RSA", None),
    "RS512": ("RSA", None),
    "PS256": ("RSA", None),
    "PS384": ("RSA", None),
    "PS512": ("RSA", None),
    "ES256": ("EC", "P-256"),
    "ES384": ("EC", "P-384"),
    "ES512": ("EC", "P-521"),
}

# RFC 9068 access-token `typ` values, refused in an ID-token header.
_ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})

# Raised by `response.json()` for a body that is not usable JSON: ValueError
# covers invalid JSON, invalid UTF-8, and an integer over the digit limit;
# RecursionError is deeply nested JSON.
_JSON_ERRORS = (ValueError, RecursionError)

# RFC 6749 section 5.2, the only token-endpoint error codes ever logged verbatim.
_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)

# Seams, read at call time.
transport: httpx.AsyncBaseTransport | None = None
clock: Callable[[], float] = time.monotonic


class OidcUnavailableError(Exception):
    """The identity provider could not be asked, or answered unusably.

    Phase 3 maps it to `sso_error=unavailable`. `str()` is fixed text naming
    only `reason`, one of this module's constants.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"single sign-on unavailable ({reason})")


class OidcRejectedError(Exception):
    """The identity provider answered and the answer is refused.

    Phase 3 maps it to `sso_error=failed`. `str()` is fixed text naming only
    `reason`, one of this module's constants.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"single sign-on refused ({reason})")


@dataclass(frozen=True)
class AuthorizationRequest:
    """One authorization request. Every field is secret to the flow (the URL
    carries `state` and `nonce`), so none is in the repr."""

    url: str = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedIdToken:
    """The claims phase 3 needs from a verified ID token.

    `email`, `username_claim`, and `name` are the configured claims when they
    are non-empty strings, else None. `email_verified` is the claim when it is
    a JSON boolean, None when it is absent or null, and False for any other
    value (a string "true" included), so a malformed claim can only refuse.
    """

    issuer: str
    subject: str
    email: str | None
    email_verified: bool | None
    username_claim: str | None
    name: str | None


@dataclass(frozen=True)
class _Discovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class _FetchFailure:
    reason: str


def _now() -> float:
    return clock()


class _ProviderCache:
    """Discovery and JWKS, each cached for an hour; failures never stored."""

    def __init__(self) -> None:
        self.discovery: SingletonTTLCache[_Discovery | _FetchFailure] = SingletonTTLCache(
            fetch=_fetch_discovery, ttl_seconds=DISCOVERY_TTL_SECONDS, clock=_now
        )
        self.jwks: SingletonTTLCache[tuple[dict[str, Any], ...] | _FetchFailure] = (
            SingletonTTLCache(fetch=_fetch_jwks, ttl_seconds=JWKS_TTL_SECONDS, clock=_now)
        )
        # When the last JWKS fetch was attempted (any outcome), for the floor.
        self.last_jwks_fetch_at: float | None = None


def reset_caches() -> None:
    """Drop the cached discovery document, the cached JWKS, and the refetch floor."""
    global _cache
    _cache = _ProviderCache()


# --- logging and errors ---


def _log_failure(action: str, reason: str, extra: dict[str, Any]) -> None:
    logger.warning(
        "single sign-on %s: %s",
        "unavailable" if action == "oidc_unavailable" else "refused",
        reason,
        extra={"action": action, "reason": reason, **extra},
    )


def _unavailable(reason: str, **extra: Any) -> OidcUnavailableError:
    _log_failure("oidc_unavailable", reason, extra)
    return OidcUnavailableError(reason)


def _rejected(reason: str, **extra: Any) -> OidcRejectedError:
    _log_failure("oidc_rejected", reason, extra)
    return OidcRejectedError(reason)


# --- configuration and HTTP ---


def _require_configuration() -> None:
    if settings.auth_method != "oidc":
        raise _unavailable("not_enabled")
    problems = oidc_configuration_problems(settings)
    if problems:
        # Variable names only, never values.
        raise _unavailable("settings_incomplete", settings=problems)


def _tls_verify() -> ssl.SSLContext | bool:
    if not settings.oidc_ca_cert:
        return True
    try:
        return ssl.create_default_context(cafile=settings.oidc_ca_cert)
    except (OSError, ValueError) as exc:
        raise _unavailable("ca_cert_unusable", error_class=type(exc).__name__) from None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        follow_redirects=False,
        verify=_tls_verify(),
        transport=transport,
    )


def _printable_ascii(value: str) -> bool:
    """ASCII with no space or control character (a URL as sent on the wire)."""
    return value.isascii() and all(0x20 < ord(ch) < 0x7F for ch in value)


def _endpoint_ok(value: object) -> bool:
    """An absolute printable-ASCII URL with a host, no userinfo, and no fragment,
    https unless not required."""
    if not isinstance(value, str) or not value or not _printable_ascii(value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    schemes = ("https",) if settings.oidc_require_https else ("https", "http")
    return (
        parts.scheme in schemes
        and bool(parts.netloc)
        and "@" not in parts.netloc
        and "#" not in value
    )


async def _get_json(url: str, failure_prefix: str) -> Any:
    """GET url and return its parsed JSON body, or a `_FetchFailure` (logged)."""
    try:
        async with asyncio.timeout(HTTP_DEADLINE_SECONDS):
            async with _client() as client:
                response = await client.get(url, headers={"Accept": "application/json"})
    except TimeoutError:
        _log_failure("oidc_unavailable", f"{failure_prefix}_deadline", {})
        return _FetchFailure(f"{failure_prefix}_deadline")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        _log_failure(
            "oidc_unavailable",
            f"{failure_prefix}_transport",
            {"error_class": type(exc).__name__},
        )
        return _FetchFailure(f"{failure_prefix}_transport")
    if response.status_code != 200:
        _log_failure(
            "oidc_unavailable",
            f"{failure_prefix}_status",
            {"status_code": response.status_code},
        )
        return _FetchFailure(f"{failure_prefix}_status")
    try:
        return response.json()
    except _JSON_ERRORS as exc:
        _log_failure(
            "oidc_unavailable",
            f"{failure_prefix}_not_json",
            {"error_class": type(exc).__name__},
        )
        return _FetchFailure(f"{failure_prefix}_not_json")


# --- discovery ---


def _discovery_url() -> str:
    issuer = settings.oidc_issuer
    if issuer.endswith("/"):
        issuer = issuer[:-1]
    return issuer + DISCOVERY_PATH


def _validate_discovery(document: Any) -> _Discovery | str:
    """The validated endpoints, or the reason the document is refused."""
    if not isinstance(document, dict):
        return "discovery_invalid"
    if document.get("issuer") != settings.oidc_issuer:
        return "discovery_issuer_mismatch"
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _endpoint_ok(document.get(name)):
            return "discovery_endpoint_invalid"
    for name, value in document.items():
        if name.endswith("_endpoint") and not _endpoint_ok(value):
            return "discovery_endpoint_invalid"
    response_types = document.get("response_types_supported")
    if not isinstance(response_types, list) or "code" not in response_types:
        return "discovery_code_flow_unsupported"
    if "code_challenge_methods_supported" in document:
        methods = document["code_challenge_methods_supported"]
        if not isinstance(methods, list) or "S256" not in methods:
            return "discovery_s256_unsupported"
    own_query = urlsplit(document["authorization_endpoint"]).query
    if any(
        name in AUTHORIZATION_PARAMETERS for name, _ in parse_qsl(own_query, keep_blank_values=True)
    ):
        return "discovery_endpoint_invalid"
    return _Discovery(
        authorization_endpoint=document["authorization_endpoint"],
        token_endpoint=document["token_endpoint"],
        jwks_uri=document["jwks_uri"],
    )


async def _fetch_discovery() -> _Discovery | Uncached[_FetchFailure]:
    document = await _get_json(_discovery_url(), "discovery")
    if isinstance(document, _FetchFailure):
        return Uncached(document)
    validated = _validate_discovery(document)
    if isinstance(validated, str):
        _log_failure("oidc_unavailable", validated, {})
        return Uncached(_FetchFailure(validated))
    return validated


async def _discovery() -> _Discovery:
    result = await _cache.discovery.get()
    if isinstance(result, _FetchFailure):
        raise OidcUnavailableError(result.reason) from None
    return result


# --- JWKS ---


async def _fetch_jwks() -> tuple[dict[str, Any], ...] | Uncached[_FetchFailure]:
    _cache.last_jwks_fetch_at = clock()
    try:
        discovery = await _discovery()
    except OidcUnavailableError as exc:
        return Uncached(_FetchFailure(exc.reason))
    document = await _get_json(discovery.jwks_uri, "jwks")
    if isinstance(document, _FetchFailure):
        return Uncached(document)
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        _log_failure("oidc_unavailable", "jwks_invalid", {})
        return Uncached(_FetchFailure("jwks_invalid"))
    try:
        # Validation only: refuses a set with no key PyJWT can load.
        jwt.PyJWKSet.from_dict({"keys": keys})
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        _log_failure("oidc_unavailable", "jwks_invalid", {"error_class": type(exc).__name__})
        return Uncached(_FetchFailure("jwks_invalid"))
    return tuple(keys)


async def _jwks() -> tuple[dict[str, Any], ...]:
    result = await _cache.jwks.get()
    if isinstance(result, _FetchFailure):
        raise OidcUnavailableError(result.reason) from None
    return result


def _key_misfit(jwk: dict[str, Any], algorithm: str) -> str | None:
    """Why a JWK cannot verify `algorithm`, or None when it can."""
    expected_kty, expected_crv = _KEY_TYPES[algorithm]
    if jwk.get("kty") != expected_kty or (
        expected_crv is not None and jwk.get("crv") != expected_crv
    ):
        return "key_type_mismatch"
    if jwk.get("use", "sig") != "sig":
        return "key_use_mismatch"
    if jwk.get("alg", algorithm) != algorithm:
        return "key_algorithm_mismatch"
    return None


def _keys_with_kid(keys: tuple[dict[str, Any], ...], kid: str) -> list[dict[str, Any]]:
    return [key for key in keys if key.get("kid") == kid]


async def _signing_key(kid: str, algorithm: str) -> jwt.PyJWK:
    """The one JWKS key that has `kid` and can verify `algorithm`.

    Keys are filtered by `kty`, `crv`, `use`, and `alg` FIRST, so a `kid`
    shared by a signing key and, say, an encryption key selects the signing
    key; two suitable keys with one `kid` are refused as ambiguous. A `kid` no
    key in the set has triggers the floored refetch; a `kid` held only by
    unsuitable keys does not (a refetch would not change the answer).
    """
    named = _keys_with_kid(await _jwks(), kid)
    if not named:
        now = clock()
        last = _cache.last_jwks_fetch_at
        if last is None or now - last >= JWKS_REFETCH_FLOOR_SECONDS:
            # No stamp here: _fetch_jwks stamps the floor as its first statement,
            # before its first suspension, and nothing is awaited between reading
            # the cache above and this check, so concurrent misses cannot both
            # pass the floor (test_misses_arriving_during_an_in_flight_fetch_...).
            _cache.jwks.invalidate()
            logger.info(
                "OIDC JWKS refetched for a key id it did not hold",
                extra={"action": "oidc_jwks_refetch"},
            )
            named = _keys_with_kid(await _jwks(), kid)
    if not named:
        raise _rejected("unknown_kid")
    suitable = [key for key in named if _key_misfit(key, algorithm) is None]
    if not suitable:
        misfits = {_key_misfit(key, algorithm) for key in named}
        raise _rejected(misfits.pop() if len(misfits) == 1 else "no_suitable_key")
    if len(suitable) > 1:
        raise _rejected("ambiguous_kid")
    try:
        return jwt.PyJWK(suitable[0], algorithm=algorithm)
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        raise _rejected("key_unusable", error_class=type(exc).__name__) from None


# --- the ID token ---


def _unverified_kid_and_alg(id_token: str) -> tuple[str, str]:
    """The header's `kid` and `alg`, read before any key is chosen.

    The one unverified read in the repository (`UNVERIFIED_READ_ALLOWED` in
    `tests/unit/test_jwt_settings_wiring.py`). Both values only select and
    check a key; the signature is verified afterwards by `decode_id_token`. A
    header whose `typ` names an RFC 9068 access token (`at+jwt`) is refused
    here too, so an access token the provider issued is never read as an ID
    token. The caller has already refused a token that is not printable ASCII.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    # RecursionError: as at the decode, PyJWT 2.15 maps it to DecodeError itself.
    except (jwt.PyJWTError, RecursionError) as exc:
        raise _rejected("malformed_token", error_class=type(exc).__name__) from None
    kid = header.get("kid")
    alg = header.get("alg")
    typ = header.get("typ")
    if isinstance(typ, str) and typ.lower() in _ACCESS_TOKEN_TYPES:
        raise _rejected("access_token_type")
    if not isinstance(kid, str) or not kid:
        raise _rejected("missing_kid")
    if not isinstance(alg, str):
        raise _rejected("algorithm_mismatch")
    return kid, alg


def _has_control_or_surrogate(value: str) -> bool:
    """Any Unicode control character (category Cc, NUL included, which a
    Postgres text column refuses) or lone surrogate (Cs, which cannot be
    encoded as UTF-8)."""
    return any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value)


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value or _has_control_or_surrogate(value):
        return None
    return value


def _email_verified(claims: dict[str, Any]) -> bool | None:
    value = claims.get("email_verified")
    if value is None:
        return None
    return value if isinstance(value, bool) else False


async def _verify_id_token(id_token: str, nonce: str) -> VerifiedIdToken:
    algorithm = settings.oidc_id_token_algorithm
    client_id = settings.oidc_client_id
    if not _printable_ascii(id_token):
        # A compact JWS is base64url and dots; anything else is refused before
        # PyJWT reads it (a lone surrogate would raise UnicodeEncodeError there).
        raise _rejected("malformed_token")
    kid, alg = _unverified_kid_and_alg(id_token)
    if alg != algorithm:
        raise _rejected("algorithm_mismatch")
    key = await _signing_key(kid, algorithm)
    try:
        claims = decode_id_token(
            id_token, key, algorithm, issuer=settings.oidc_issuer, audience=client_id
        )
    except jwt.InvalidKeyError as exc:
        # With a key already built by PyJWK, only the minimum-key-length check
        # (an RSA key under 2048 bits) raises this at decode time.
        raise _rejected("key_too_short", error_class=type(exc).__name__) from None
    except (jwt.PyJWTError, RecursionError) as exc:
        # PyJWT 2.15 already turns a RecursionError from a deeply nested payload
        # into DecodeError; caught here too so another PyJWT cannot let it escape.
        raise _rejected("id_token_invalid", error_class=type(exc).__name__) from None

    audience = claims["aud"]
    audiences = [audience] if isinstance(audience, str) else list(audience)
    azp = claims.get("azp")
    if azp is None and len(audiences) > 1:
        raise _rejected("azp_missing")
    if azp is not None and azp != client_id:
        raise _rejected("azp_mismatch")

    claimed_nonce = claims.get("nonce")
    # HERD's nonces are base64url, so a non-ASCII claim cannot match (and could
    # not be encoded for the compare if it held a lone surrogate).
    if (
        not isinstance(claimed_nonce, str)
        or not claimed_nonce.isascii()
        or not hmac.compare_digest(claimed_nonce.encode("ascii"), nonce.encode("utf-8"))
    ):
        raise _rejected("nonce_mismatch")

    subject = claims.get("sub")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > MAX_SUBJECT_LENGTH
        or _has_control_or_surrogate(subject)
    ):
        raise _rejected("subject_invalid")

    return VerifiedIdToken(
        issuer=claims["iss"],
        subject=subject,
        email=_optional_string(claims.get(settings.oidc_email_claim)),
        email_verified=_email_verified(claims),
        username_claim=_optional_string(claims.get(settings.oidc_username_claim)),
        name=_optional_string(claims.get("name")),
    )


# --- the code exchange ---


def _oauth_error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except _JSON_ERRORS:
        return "other"
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) and error in _OAUTH_ERROR_CODES else "other"


async def _exchange_code(token_endpoint: str, code: str, code_verifier: str) -> str:
    """POST the code to the token endpoint and return the `id_token` it answers."""
    auth = httpx.BasicAuth(
        quote_plus(settings.oidc_client_id), quote_plus(settings.oidc_client_secret)
    )
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        async with asyncio.timeout(HTTP_DEADLINE_SECONDS):
            async with _client() as client:
                response = await client.post(
                    token_endpoint, data=form, auth=auth, headers={"Accept": "application/json"}
                )
    except TimeoutError:
        raise _unavailable("token_deadline") from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _unavailable("token_transport", error_class=type(exc).__name__) from None
    status = response.status_code
    if 400 <= status < 500:
        raise _rejected(
            "token_refused", status_code=status, oauth_error=_oauth_error_code(response)
        )
    if status != 200:
        raise _unavailable("token_status", status_code=status)
    try:
        body = response.json()
    except _JSON_ERRORS as exc:
        raise _unavailable("token_not_json", error_class=type(exc).__name__) from None
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise _rejected("no_id_token")
    return id_token


# --- the entry points ---


def _code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def build_authorization_request() -> AuthorizationRequest:
    """A fresh state, nonce, and PKCE verifier, and the URL that carries them.

    Raises `OidcUnavailableError` when not in OIDC mode, when settings are
    incomplete, or when discovery cannot be fetched or validated.
    """
    _require_configuration()
    discovery = await _discovery()
    state = secrets.token_urlsafe(STATE_BYTES)
    nonce = secrets.token_urlsafe(NONCE_BYTES)
    code_verifier = "".join(
        secrets.choice(CODE_VERIFIER_ALPHABET) for _ in range(CODE_VERIFIER_LENGTH)
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri,
            "scope": settings.oidc_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": _code_challenge(code_verifier),
            "code_challenge_method": "S256",
            "response_mode": "query",
        }
    )
    parts = urlsplit(discovery.authorization_endpoint)
    if parts.query:
        query = f"{parts.query}&{query}"
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    return AuthorizationRequest(url=url, state=state, nonce=nonce, code_verifier=code_verifier)


async def redeem_code(code: str, code_verifier: str, nonce: str) -> VerifiedIdToken:
    """Exchange an authorization code and return the verified ID token's claims.

    Fetches discovery and JWKS (cached), posts the code with the verifier and
    the client secret, and verifies the ID token against `nonce`. Raises
    `OidcUnavailableError` or `OidcRejectedError`, never with IdP text.
    """
    _require_configuration()
    if not code or not code_verifier or not nonce:
        raise _rejected("missing_input")
    discovery = await _discovery()
    id_token = await _exchange_code(discovery.token_endpoint, code, code_verifier)
    return await _verify_id_token(id_token, nonce)


_cache = _ProviderCache()
