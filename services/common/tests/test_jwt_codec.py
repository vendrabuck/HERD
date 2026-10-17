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
"""

import base64
import json
import logging
import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from herd_common import jwt_codec
from herd_common.auth import make_auth_dependencies
from herd_common.jwt_codec import ACCESS_TOKEN_LEEWAY_SECONDS, decode_access_token
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
