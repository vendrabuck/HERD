import base64
import json
import logging
import time
import uuid

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from herd_common import auth as auth_module
from herd_common.auth import caller_id, make_auth_dependencies
from herd_common.logging import JSONFormatter

SECRET = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
OTHER_SECRET = "c1d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8"
ALG = "HS256"

get_current_user_payload, require_admin = make_auth_dependencies(SECRET, ALG)


def _make_token(payload: dict, secret: str = SECRET) -> str:
    return jwt.encode(payload, secret, algorithm=ALG)


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# --- Factory contract ---


def test_make_auth_dependencies_returns_two_callables():
    a, b = make_auth_dependencies("s")
    assert callable(a)
    assert callable(b)


# --- get_current_user_payload ---


def test_valid_token_returns_payload():
    token = _make_token({"sub": "user-1", "role": "admin"})
    result = get_current_user_payload(_creds(token))
    assert result["sub"] == "user-1"
    assert result["role"] == "admin"


def test_expired_token_raises_401():
    token = _make_token({"sub": "user-1", "exp": int(time.time()) - 60})
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401


def test_invalid_signature_raises_401():
    token = _make_token({"sub": "user-1"}, secret=OTHER_SECRET)
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401


def test_missing_sub_raises_401():
    token = _make_token({"role": "admin"})
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401


def test_empty_sub_raises_401():
    token = _make_token({"sub": "", "role": "admin"})
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401


def test_malformed_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds("not.a.jwt"))
    assert exc_info.value.status_code == 401


# --- algorithm list and key errors ---

# PyJWT refuses this as an HMAC secret (it is a PEM public key block) and raises
# InvalidKeyError, a PyJWTError that is not an InvalidTokenError, while preparing
# the key.
ASYMMETRIC_SHAPED_KEY = (
    "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQc=\n-----END PUBLIC KEY-----\n"
)


def _unsigned_token(payload: dict) -> str:
    def _segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(payload)}."


def test_token_with_alg_none_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(_unsigned_token({"sub": "user-1", "role": "admin"})))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_token_signed_with_another_hmac_algorithm_raises_401():
    token = jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS512")
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_key_that_pyjwt_cannot_use_raises_401_not_a_key_error():
    payload_dep, _ = make_auth_dependencies(ASYMMETRIC_SHAPED_KEY, ALG)
    token = _make_token({"sub": "user-1"})
    with pytest.raises(HTTPException) as exc_info:
        payload_dep(_creds(token))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def _record_decode_algorithms(monkeypatch) -> list:
    """Wrap PyJWT's decode as herd_common.auth reaches it through the shared
    helper; record `algorithms`."""
    seen: list = []
    real_decode = auth_module.jwt.decode

    def recording_decode(*args, **kwargs):
        seen.append(kwargs.get("algorithms"))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(auth_module.jwt, "decode", recording_decode)
    return seen


@pytest.mark.parametrize("algorithm", ["HS256", "HS512"])
def test_decode_is_given_exactly_the_configured_algorithm(monkeypatch, algorithm):
    seen = _record_decode_algorithms(monkeypatch)
    payload_dep, _ = make_auth_dependencies(SECRET, algorithm)
    token = jwt.encode({"sub": "user-1"}, SECRET, algorithm=algorithm)
    assert payload_dep(_creds(token))["sub"] == "user-1"
    with pytest.raises(HTTPException):
        payload_dep(_creds(_unsigned_token({"sub": "user-1"})))
    assert seen == [[algorithm], [algorithm]]


def test_key_error_is_logged_once_by_class_name(caplog):
    caplog.set_level(logging.DEBUG, logger="herd_common.auth")
    payload_dep, _ = make_auth_dependencies(ASYMMETRIC_SHAPED_KEY, ALG)
    with pytest.raises(HTTPException):
        payload_dep(_creds(_make_token({"sub": "user-1"})))
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
    assert "HMAC" not in formatted


@pytest.mark.parametrize("shape", ["alg-none", "hs512", "wrong-key", "garbage"])
def test_a_token_that_does_not_verify_is_not_logged(caplog, shape):
    caplog.set_level(logging.DEBUG, logger="herd_common.auth")
    token = {
        "alg-none": _unsigned_token({"sub": "user-1"}),
        "hs512": jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS512"),
        "wrong-key": _make_token({"sub": "user-1"}, secret=OTHER_SECRET),
        "garbage": "not.a.token",
    }[shape]
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401
    assert [r for r in caplog.records if r.name == "herd_common.auth"] == []


def test_hs512_service_accepts_its_own_tokens_only():
    payload_dep, _ = make_auth_dependencies(SECRET, "HS512")
    good = jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS512")
    assert payload_dep(_creds(good))["sub"] == "user-1"
    with pytest.raises(HTTPException) as exc_info:
        payload_dep(_creds(_make_token({"sub": "user-1"})))
    assert exc_info.value.status_code == 401


# --- require_admin ---


def test_require_admin_with_admin_role():
    payload = {"sub": "user-1", "role": "admin"}
    result = require_admin(payload)
    assert result["sub"] == "user-1"


def test_require_admin_with_superadmin_role():
    payload = {"sub": "user-1", "role": "superadmin"}
    result = require_admin(payload)
    assert result["sub"] == "user-1"


def test_require_admin_with_user_role_raises_403():
    payload = {"sub": "user-1", "role": "user"}
    with pytest.raises(HTTPException) as exc_info:
        require_admin(payload)
    assert exc_info.value.status_code == 403


# --- caller_id ---


def test_caller_id_parses_valid_uuid_subject():
    user_id = uuid.uuid4()
    result = caller_id({"sub": str(user_id)})
    assert result == user_id


def test_caller_id_rejects_missing_subject():
    with pytest.raises(HTTPException) as exc_info:
        caller_id({})
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid subject in token"


def test_caller_id_rejects_non_uuid_subject():
    with pytest.raises(HTTPException) as exc_info:
        caller_id({"sub": "not-a-uuid"})
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid subject in token"


def test_caller_id_rejects_non_string_subject():
    with pytest.raises(HTTPException) as exc_info:
        caller_id({"sub": None})
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid subject in token"
