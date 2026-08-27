import time
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from herd_common.auth import caller_id, make_auth_dependencies
from jose import jwt

SECRET = "test-secret"
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
    token = _make_token({"sub": "user-1", "exp": int(time.time()) - 10})
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_payload(_creds(token))
    assert exc_info.value.status_code == 401


def test_invalid_signature_raises_401():
    token = _make_token({"sub": "user-1"}, secret="wrong-secret")
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
