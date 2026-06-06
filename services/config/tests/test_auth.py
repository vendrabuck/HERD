"""Tests for app.auth session token creation and verification."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from app.auth import _ALGORITHM, _CONFIG_SESSION_SECRET, _verify_token, create_session_token
from fastapi import HTTPException


def test_create_session_token_is_decodable():
    token = create_session_token()
    decoded = jwt.decode(token, _CONFIG_SESSION_SECRET, algorithms=[_ALGORITHM])
    assert decoded["sub"] == "config-admin"


def test_verify_token_accepts_valid_token():
    token = create_session_token()
    assert _verify_token(token)["sub"] == "config-admin"


def test_verify_token_rejects_expired_token():
    payload = {
        "sub": "config-admin",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        "iat": datetime.now(timezone.utc) - timedelta(minutes=31),
    }
    expired = jwt.encode(payload, _CONFIG_SESSION_SECRET, algorithm=_ALGORITHM)
    with pytest.raises(HTTPException) as exc_info:
        _verify_token(expired)
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


def test_verify_token_rejects_invalid_token():
    with pytest.raises(HTTPException) as exc_info:
        _verify_token("not-a-real-jwt")
    assert exc_info.value.status_code == 401
    assert "invalid" in exc_info.value.detail.lower()
