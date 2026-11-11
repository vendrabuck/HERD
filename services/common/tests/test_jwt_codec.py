"""herd_common.jwt_codec.decode_access_token, the one access-token decode, and the
401 every failure becomes through `make_auth_dependencies` (issue #1186).

Two layers are pinned. The helper: each failure mode raises one exact PyJWT class
with its exact message, the 10 s leeway applies to `exp` and `iat`, a short key
verifies with exactly one `InsecureKeyLengthWarning` (`enforce_minimum_key_length`
stays off), a key PyJWT cannot use raises `InvalidKeyError` (a `PyJWTError` that
is not an `InvalidTokenError`), and a token the previous library signed still
verifies.
Over HTTP: every one of those failures answers 401 `Could not validate
credentials` with `WWW-Authenticate: Bearer`, and only the key error is logged.

`decode_id_token` (ADR 0016, the OpenID Connect ID-token decode) is pinned at the
bottom: it verifies under each of the nine asymmetric algorithms, passes exactly
one algorithm with the issuer, audience, leeway, and required claims, refuses
every HS algorithm and `none` before decoding (an HMAC token keyed by the client
secret or by the public key PEM included), and each claim failure raises its
exact class from a token that otherwise verifies. Keys are generated per session.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from herd_common import jwt_codec
from herd_common.auth import make_auth_dependencies
from herd_common.jwt_codec import (
    ACCESS_TOKEN_LEEWAY_SECONDS,
    ID_TOKEN_ALGORITHMS,
    ID_TOKEN_REQUIRED_CLAIMS,
    decode_access_token,
    decode_id_token,
)
from herd_common.logging import JSONFormatter
from httpx import ASGITransport, AsyncClient
from jwt.warnings import InsecureKeyLengthWarning

KEY = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
OTHER_KEY = "c1d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8"
ALG = "HS256"
DETAIL = "Could not validate credentials"

# A PEM public key block: PyJWT's HMAC key preparation refuses it with
# InvalidKeyError before any signature is checked.
PEM_KEY = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQc=\n-----END PUBLIC KEY-----\n"

# Signed with the JWT library HERD used before PyJWT (version 3.5.0, its
# `jwt.encode(claims, INTEROP_KEY, algorithm="HS256")`), before it left the lock
# (issue #1186), so a session issued by a build still on that library verifies
# under this one during a rolling deploy.
INTEROP_KEY = "0123456789abcdef0123456789abcdef"
INTEROP_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIzZjFjMmE5ZS0wMDAwLTQwMDAtODAwMC0wMDAwMDAwMDAwMDEiLCJyb2xlIjoiYWRtaW4iLCJleH"
    "AiOjQwNzA5MDg4MDAsImlhdCI6MTc5MjEwODgwMH0."
    "PDiOIkYFIPdoeSrR4j0sXA4lRftSDPoNJ8wsINRhXG0"
)
INTEROP_CLAIMS = {
    "sub": "3f1c2a9e-0000-4000-8000-000000000001",
    "role": "admin",
    "exp": 4070908800,
    "iat": 1792108800,
}


def _now() -> int:
    return int(time.time())


def _token(claims: dict, key: str = KEY, algorithm: str = ALG) -> str:
    return jwt.encode(claims, key, algorithm=algorithm)


def _unsigned_token(claims: dict) -> str:
    def _segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(claims)}."


# --- the helper ---


def test_leeway_is_ten_seconds():
    assert ACCESS_TOKEN_LEEWAY_SECONDS == 10


def test_a_good_token_returns_its_claims():
    claims = {"sub": "user-1", "role": "admin", "exp": _now() + 300}
    assert decode_access_token(_token(claims), KEY, ALG) == claims


def test_decode_is_given_one_algorithm_and_the_leeway(monkeypatch):
    seen: list = []
    real_decode = jwt_codec.jwt.decode

    def recording_decode(*args, **kwargs):
        seen.append((args[1:], kwargs))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(jwt_codec.jwt, "decode", recording_decode)
    decode_access_token(_token({"sub": "user-1"}, algorithm="HS512"), KEY, "HS512")
    assert seen == [((KEY,), {"algorithms": ["HS512"], "leeway": 10})]


# Each failure: (token builder, exact class, exact message). The builders run at
# test time so the clock-relative claims are fresh.
_FAILURES = {
    "expired": (
        lambda: _token({"sub": "user-1", "exp": _now() - 60}),
        jwt.ExpiredSignatureError,
        "Signature has expired",
    ),
    "wrong-key": (
        lambda: _token({"sub": "user-1"}, key=OTHER_KEY),
        jwt.InvalidSignatureError,
        "Signature verification failed",
    ),
    "wrong-algorithm": (
        lambda: _token({"sub": "user-1"}, algorithm="HS512"),
        jwt.InvalidAlgorithmError,
        "The specified alg value is not allowed",
    ),
    "alg-none": (
        lambda: _unsigned_token({"sub": "user-1"}),
        jwt.InvalidAlgorithmError,
        "The specified alg value is not allowed",
    ),
    "garbage": (lambda: "not.a.token", jwt.DecodeError, "Invalid header padding"),
    "non-string-sub": (
        lambda: _token({"sub": 5}),
        jwt.exceptions.InvalidSubjectError,
        "Subject must be a string",
    ),
    # 12 s, not 11: one second of margin against the clock tick, so a leeway of
    # 11 to 29 s fails here and not only in the constant pins.
    "exp-just-past-the-leeway": (
        lambda: _token({"sub": "user-1", "exp": _now() - 12}),
        jwt.ExpiredSignatureError,
        "Signature has expired",
    ),
    "iat-just-past-the-leeway": (
        lambda: _token({"sub": "user-1", "iat": _now() + 12}),
        jwt.ImmatureSignatureError,
        "The token is not yet valid (iat)",
    ),
    "iat-past-the-leeway": (
        lambda: _token({"sub": "user-1", "iat": _now() + 30}),
        jwt.ImmatureSignatureError,
        "The token is not yet valid (iat)",
    ),
}


@pytest.mark.parametrize("name", sorted(_FAILURES))
def test_each_failure_raises_its_exact_class_and_message(name):
    build, error_class, message = _FAILURES[name]
    with pytest.raises(jwt.PyJWTError) as exc_info:
        decode_access_token(build(), KEY, ALG)
    assert type(exc_info.value) is error_class
    assert str(exc_info.value) == message
    assert isinstance(exc_info.value, jwt.InvalidTokenError)


def test_a_token_without_sub_decodes_and_the_caller_refuses_it():
    # PyJWT does not require `sub`; every decode site refuses a missing one.
    assert decode_access_token(_token({"role": "admin"}), KEY, ALG) == {"role": "admin"}


def test_an_iat_inside_the_leeway_verifies():
    iat = _now() + 5
    assert decode_access_token(_token({"sub": "user-1", "iat": iat}), KEY, ALG) == {
        "sub": "user-1",
        "iat": iat,
    }


def test_an_exp_inside_the_leeway_still_verifies():
    exp = _now() - 5
    assert decode_access_token(_token({"sub": "user-1", "exp": exp}), KEY, ALG) == {
        "sub": "user-1",
        "exp": exp,
    }


def test_a_short_key_verifies_with_one_insecure_key_length_warning():
    short_key = "s" * 16
    with pytest.warns(InsecureKeyLengthWarning):
        token = _token({"sub": "user-1"}, key=short_key)
    with pytest.warns(InsecureKeyLengthWarning) as record:
        claims = decode_access_token(token, short_key, ALG)
    assert claims == {"sub": "user-1"}
    assert [str(w.message) for w in record] == [
        "The HMAC key is 16 bytes long, which is below the minimum recommended length "
        "of 32 bytes for SHA256. See RFC 7518 Section 3.2."
    ]


def test_a_key_pyjwt_cannot_use_raises_invalid_key_error_not_a_token_error():
    with pytest.raises(jwt.PyJWTError) as exc_info:
        decode_access_token(_token({"sub": "user-1"}), PEM_KEY, ALG)
    assert type(exc_info.value) is jwt.InvalidKeyError
    assert not isinstance(exc_info.value, jwt.InvalidTokenError)


def test_a_token_the_previous_library_signed_verifies():
    assert decode_access_token(INTEROP_TOKEN, INTEROP_KEY, ALG) == INTEROP_CLAIMS


# --- over HTTP, through make_auth_dependencies ---


def _app(key: str = KEY) -> FastAPI:
    get_payload, _ = make_auth_dependencies(key, ALG)
    app = FastAPI()

    @app.get("/whoami")
    def whoami(payload: dict = Depends(get_payload)) -> dict:
        return {"sub": payload["sub"]}

    return app


async def _get(app: FastAPI, token: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get("/whoami", headers={"Authorization": f"Bearer {token}"})


async def test_a_good_token_answers_200_over_http():
    resp = await _get(_app(), _token({"sub": "user-1", "iat": _now() + 5}))
    assert resp.status_code == 200
    assert resp.json() == {"sub": "user-1"}


_HTTP_FAILURES = sorted(_FAILURES) + ["missing-sub"]


@pytest.mark.parametrize("name", _HTTP_FAILURES)
async def test_each_failure_answers_401_over_http_and_is_not_logged(name, caplog):
    caplog.set_level(logging.DEBUG, logger="herd_common.auth")
    token = _token({"role": "admin"}) if name == "missing-sub" else _FAILURES[name][0]()
    resp = await _get(_app(), token)
    assert resp.status_code == 401
    assert resp.json() == {"detail": DETAIL}
    assert resp.headers["www-authenticate"] == "Bearer"
    assert [r for r in caplog.records if r.name == "herd_common.auth"] == []


async def test_a_key_pyjwt_cannot_use_answers_401_over_http_and_is_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="herd_common.auth")
    resp = await _get(_app(PEM_KEY), _token({"sub": "user-1"}))
    assert resp.status_code == 401
    assert resp.json() == {"detail": DETAIL}
    records = [r for r in caplog.records if r.name == "herd_common.auth"]
    assert len(records) == 1
    formatted = JSONFormatter("common").format(records[0])
    logged = json.loads(formatted)
    assert logged["level"] == "ERROR"
    assert logged["message"] == (
        "access token not checked: the configured key is unusable (InvalidKeyError)"
    )
    assert logged["action"] == "jwt_key_unusable"
    assert logged["error_class"] == "InvalidKeyError"
    assert "BEGIN PUBLIC KEY" not in formatted


async def test_a_short_key_answers_200_over_http_with_the_warning():
    short_key = "s" * 16
    with pytest.warns(InsecureKeyLengthWarning):
        token = _token({"sub": "user-1"}, key=short_key)
    with pytest.warns(InsecureKeyLengthWarning):
        resp = await _get(_app(short_key), token)
    assert resp.status_code == 200
    assert resp.json() == {"sub": "user-1"}


async def test_a_token_the_previous_library_signed_answers_200_over_http():
    resp = await _get(_app(INTEROP_KEY), INTEROP_TOKEN)
    assert resp.status_code == 200
    assert resp.json() == {"sub": INTEROP_CLAIMS["sub"]}


# --- decode_id_token, the OpenID Connect ID-token decode (ADR 0016, issue #37) ---
#
# Keys are generated in the test session; no key material is committed.

ISSUER = "https://idp.test/realms/herd"
CLIENT_ID = "herd-client"
CLIENT_SECRET = "client-secret-0123456789abcdef0123456789"
NONCE = "nonce-value-for-the-flow"


@pytest.fixture(scope="module")
def id_keys() -> dict:
    return {
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "other_rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "P-256": ec.generate_private_key(ec.SECP256R1()),
        "P-384": ec.generate_private_key(ec.SECP384R1()),
        "P-521": ec.generate_private_key(ec.SECP521R1()),
    }


def _key_for(algorithm: str, keys: dict):
    curves = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
    return keys[curves[algorithm]] if algorithm in curves else keys["rsa"]


def _id_claims(**changes) -> dict:
    now = _now()
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "subject-1",
        "exp": now + 300,
        "iat": now,
        "nonce": NONCE,
    }
    for name, value in changes.items():
        if value is _DROP:
            claims.pop(name)
        else:
            claims[name] = value
    return claims


_DROP = object()


def _id_token(claims: dict, private_key, algorithm: str = "RS256") -> str:
    return jwt.encode(claims, private_key, algorithm=algorithm)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forged_hmac_token(claims: dict, secret: bytes, algorithm: str = "HS256") -> str:
    """An HMAC token built by hand, as an attacker would: PyJWT's own encode
    refuses a PEM block as an HMAC secret, so it cannot build this one."""
    digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}
    header = _b64(json.dumps({"alg": algorithm, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(secret, signing_input, digest[algorithm]).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def _public(private_key):
    return private_key.public_key()


def _public_pem(private_key) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _decode_id(token: str, key, algorithm: str = "RS256") -> dict:
    return decode_id_token(token, key, algorithm, issuer=ISSUER, audience=CLIENT_ID)


def test_id_token_algorithms_are_exactly_the_nine_asymmetric_ones():
    assert ID_TOKEN_ALGORITHMS == (
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
    )


def test_id_token_required_claims_are_the_adr_six():
    assert ID_TOKEN_REQUIRED_CLAIMS == ("exp", "iat", "iss", "aud", "sub", "nonce")


@pytest.mark.parametrize("algorithm", ID_TOKEN_ALGORITHMS)
def test_a_good_id_token_verifies_under_each_algorithm(algorithm, id_keys):
    private_key = _key_for(algorithm, id_keys)
    claims = _id_claims()
    token = _id_token(claims, private_key, algorithm)
    assert _decode_id(token, _public(private_key), algorithm) == claims


def test_a_good_id_token_verifies_with_a_pyjwk_key(id_keys):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_public(id_keys["rsa"])))
    claims = _id_claims()
    token = _id_token(claims, id_keys["rsa"])
    assert _decode_id(token, jwt.PyJWK(jwk, algorithm="RS256")) == claims


def test_id_decode_is_given_one_algorithm_issuer_audience_leeway_and_required_claims(
    monkeypatch, id_keys
):
    seen: list = []
    real_decode = jwt_codec.jwt.decode

    def recording_decode(*args, **kwargs):
        seen.append((args[1:], kwargs))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(jwt_codec.jwt, "decode", recording_decode)
    key = _public(id_keys["rsa"])
    _decode_id(_id_token(_id_claims(), id_keys["rsa"]), key)
    assert seen == [
        (
            (key,),
            {
                "algorithms": ["RS256"],
                "issuer": ISSUER,
                "audience": CLIENT_ID,
                "leeway": 10,
                "options": {
                    "require": ["exp", "iat", "iss", "aud", "sub", "nonce"],
                    "enforce_minimum_key_length": True,
                },
            },
        )
    ]


# Algorithms a caller might pass that the helper refuses before any decode.
_REFUSED_ID_ALGORITHMS = ["HS256", "HS384", "HS512", "none", "None", "", "rs256", "EdDSA"]


@pytest.mark.parametrize("algorithm", _REFUSED_ID_ALGORITHMS)
def test_id_decode_refuses_a_non_asymmetric_algorithm_before_decoding(
    algorithm, monkeypatch, id_keys
):
    calls: list = []
    monkeypatch.setattr(jwt_codec.jwt, "decode", lambda *a, **k: calls.append(1))
    token = _forged_hmac_token(_id_claims(), CLIENT_SECRET.encode())
    with pytest.raises(jwt.InvalidAlgorithmError) as exc_info:
        decode_id_token(token, CLIENT_SECRET, algorithm, issuer=ISSUER, audience=CLIENT_ID)
    assert str(exc_info.value) == "The specified alg value is not allowed"
    assert calls == []


@pytest.mark.parametrize("hmac_algorithm", ["HS256", "HS384", "HS512"])
def test_id_decode_refuses_an_hmac_token_keyed_by_the_client_secret(hmac_algorithm, id_keys):
    token = _forged_hmac_token(_id_claims(), CLIENT_SECRET.encode(), hmac_algorithm)
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode_id(token, _public(id_keys["rsa"]))
    # Even handed the client secret itself as the key.
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode_id(token, CLIENT_SECRET)


def test_id_decode_refuses_an_hmac_token_keyed_by_the_public_key_pem(id_keys):
    pem = _public_pem(id_keys["rsa"])
    token = _forged_hmac_token(_id_claims(), pem)
    for key in (pem, pem.decode(), _public(id_keys["rsa"])):
        with pytest.raises(jwt.InvalidAlgorithmError):
            _decode_id(token, key)


def test_id_decode_refuses_alg_none(id_keys):
    token = _unsigned_token(_id_claims())
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode_id(token, _public(id_keys["rsa"]))


def test_id_decode_refuses_another_asymmetric_algorithm_than_the_pinned_one(id_keys):
    token = _id_token(_id_claims(), id_keys["rsa"], "RS384")
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode_id(token, _public(id_keys["rsa"]), "RS256")
    token = _id_token(_id_claims(), id_keys["rsa"], "PS256")
    with pytest.raises(jwt.InvalidAlgorithmError):
        _decode_id(token, _public(id_keys["rsa"]), "RS256")


def test_id_decode_refuses_an_empty_expected_issuer_or_audience(id_keys):
    claims = _id_claims(iss="", aud="")
    token = _id_token(claims, id_keys["rsa"])
    key = _public(id_keys["rsa"])
    with pytest.raises(jwt.InvalidIssuerError):
        decode_id_token(token, key, "RS256", issuer="", audience=CLIENT_ID)
    with pytest.raises(jwt.InvalidAudienceError):
        decode_id_token(token, key, "RS256", issuer=ISSUER, audience="")


# Each refusal flips exactly one thing of a token that otherwise verifies
# (test_a_good_id_token_verifies_under_each_algorithm): (claim changes, signing
# key name, exact class, exact message).
_ID_FAILURES = {
    "wrong-signing-key": (
        {},
        "other_rsa",
        jwt.InvalidSignatureError,
        "Signature verification failed",
    ),
    "iss-different": (
        {"iss": "https://evil.test/realms/herd"},
        "rsa",
        jwt.InvalidIssuerError,
        "Invalid issuer",
    ),
    "iss-trailing-slash": ({"iss": ISSUER + "/"}, "rsa", jwt.InvalidIssuerError, "Invalid issuer"),
    "iss-missing": (
        {"iss": _DROP},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "iss" claim',
    ),
    "aud-different": (
        {"aud": "other-client"},
        "rsa",
        jwt.InvalidAudienceError,
        "Audience doesn't match",
    ),
    "aud-list-without-client": (
        {"aud": ["other-client", "third"]},
        "rsa",
        jwt.InvalidAudienceError,
        "Audience doesn't match",
    ),
    "aud-missing": (
        {"aud": _DROP},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "aud" claim',
    ),
    "exp-past-the-leeway": (
        {"exp": "PAST12"},
        "rsa",
        jwt.ExpiredSignatureError,
        "Signature has expired",
    ),
    "exp-missing": (
        {"exp": _DROP},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "exp" claim',
    ),
    "iat-past-the-leeway": (
        {"iat": "FUTURE12"},
        "rsa",
        jwt.ImmatureSignatureError,
        "The token is not yet valid (iat)",
    ),
    "iat-missing": (
        {"iat": _DROP},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "iat" claim',
    ),
    "nbf-past-the-leeway": (
        {"nbf": "FUTURE12"},
        "rsa",
        jwt.ImmatureSignatureError,
        "The token is not yet valid (nbf)",
    ),
    "sub-missing": (
        {"sub": _DROP},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "sub" claim',
    ),
    "sub-not-a-string": (
        {"sub": 7},
        "rsa",
        jwt.exceptions.InvalidSubjectError,
        "Subject must be a string",
    ),
    "nonce-missing": (
        {"nonce": _DROP},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "nonce" claim',
    ),
    "nonce-null": (
        {"nonce": None},
        "rsa",
        jwt.MissingRequiredClaimError,
        'Token is missing the "nonce" claim',
    ),
}


def _resolve_times(changes: dict) -> dict:
    now = _now()
    marks = {"PAST12": now - 12, "FUTURE12": now + 12}
    return {k: marks.get(v, v) if isinstance(v, str) else v for k, v in changes.items()}


@pytest.mark.parametrize("name", sorted(_ID_FAILURES))
def test_each_id_token_failure_raises_its_exact_class(name, id_keys):
    changes, signer, error_class, message = _ID_FAILURES[name]
    token = _id_token(_id_claims(**_resolve_times(changes)), id_keys[signer])
    with pytest.raises(jwt.PyJWTError) as exc_info:
        _decode_id(token, _public(id_keys["rsa"]))
    assert type(exc_info.value) is error_class
    assert str(exc_info.value) == message


def test_a_tampered_id_token_payload_is_refused(id_keys):
    header, _, signature = _id_token(_id_claims(), id_keys["rsa"]).split(".")
    forged = _b64(json.dumps(_id_claims(sub="someone-else")).encode())
    with pytest.raises(jwt.InvalidSignatureError):
        _decode_id(f"{header}.{forged}.{signature}", _public(id_keys["rsa"]))


@pytest.mark.parametrize(
    "changes",
    [{"exp": -5}, {"iat": 5}, {"nbf": 5}, {"aud": [CLIENT_ID]}, {"aud": [CLIENT_ID, "other"]}],
    ids=["exp-inside-leeway", "iat-inside-leeway", "nbf-inside-leeway", "aud-list", "aud-multi"],
)
def test_id_token_inside_the_leeway_or_with_an_aud_list_verifies(changes, id_keys):
    now = _now()
    resolved = {k: (now + v if k in ("exp", "iat", "nbf") else v) for k, v in changes.items()}
    claims = _id_claims(**resolved)
    token = _id_token(claims, id_keys["rsa"])
    assert _decode_id(token, _public(id_keys["rsa"])) == claims


def test_id_decode_refuses_an_rsa_key_under_2048_bits(id_keys):
    """PyJWT's minimum key length is ON for ID tokens: a 1024-bit provider key
    raises InvalidKeyError instead of verifying with a warning (the access-token
    decode keeps it off). Signing with the short key warns, as PyJWT does."""
    short_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.warns(InsecureKeyLengthWarning):
        token = _id_token(_id_claims(), short_key)
    with pytest.raises(jwt.PyJWTError) as exc_info:
        _decode_id(token, _public(short_key))
    assert type(exc_info.value) is jwt.InvalidKeyError
    assert str(exc_info.value) == (
        "The RSA key is 1024 bits long, which is below the minimum recommended size "
        "of 2048 bits. See NIST SP 800-131A."
    )


def test_id_decode_accepts_an_rsa_key_of_exactly_2048_bits(id_keys):
    claims = _id_claims()
    assert id_keys["rsa"].key_size == 2048
    assert _decode_id(_id_token(claims, id_keys["rsa"]), _public(id_keys["rsa"])) == claims
