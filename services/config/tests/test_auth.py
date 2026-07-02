"""Tests for app.auth session token creation and verification."""

import importlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from app.auth import _ALGORITHM, _CONFIG_SESSION_SECRET, _verify_token, create_session_token
from fastapi import HTTPException

# The forgeable constant that used to sign and verify every config session
# (issue #246). It must never verify against the runtime secret again.
_OLD_FORGEABLE_SECRET = "herd-config-session-internal-key"


def test_create_session_token_round_trips():
    # Signed with the service's actual runtime secret; verify must pass.
    token = create_session_token()
    decoded = jwt.decode(token, _CONFIG_SESSION_SECRET, algorithms=[_ALGORITHM])
    assert decoded["sub"] == "config-admin"


def test_verify_token_accepts_valid_token():
    token = create_session_token()
    assert _verify_token(token)["sub"] == "config-admin"


def test_verify_token_rejects_token_signed_with_old_forgeable_constant():
    # Regression for issue #246: a token signed with the source-visible
    # constant must be rejected, so it can no longer forge a config session.
    payload = {
        "sub": "config-admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "iat": datetime.now(timezone.utc),
    }
    forged = jwt.encode(payload, _OLD_FORGEABLE_SECRET, algorithm=_ALGORITHM)
    # The runtime secret must not equal the old constant.
    assert _CONFIG_SESSION_SECRET != _OLD_FORGEABLE_SECRET
    with pytest.raises(HTTPException) as exc_info:
        _verify_token(forged)
    assert exc_info.value.status_code == 401
    assert "invalid" in exc_info.value.detail.lower()


def test_verify_token_rejects_bad_signature():
    # A token with a valid payload but signed by an unknown key is rejected.
    payload = {
        "sub": "config-admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "iat": datetime.now(timezone.utc),
    }
    bad = jwt.encode(payload, "some-other-random-secret-at-least-32b", algorithm=_ALGORITHM)
    with pytest.raises(HTTPException) as exc_info:
        _verify_token(bad)
    assert exc_info.value.status_code == 401
    assert "invalid" in exc_info.value.detail.lower()


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


def test_config_session_secret_env_var_is_honored(monkeypatch):
    # An operator can pin the secret across replicas via the env var; tokens
    # signed with that value must verify after the module reloads.
    pinned = "operator-pinned-secret-value-at-least-32-bytes-long"
    monkeypatch.setenv("CONFIG_SESSION_SECRET", pinned)
    import app.auth as auth_module

    reloaded = importlib.reload(auth_module)
    try:
        assert reloaded._CONFIG_SESSION_SECRET == pinned
        token = reloaded.create_session_token()
        assert jwt.decode(token, pinned, algorithms=[reloaded._ALGORITHM])["sub"] == "config-admin"
        assert reloaded._verify_token(token)["sub"] == "config-admin"
    finally:
        # Restore the original (random) per-process secret for other tests.
        monkeypatch.delenv("CONFIG_SESSION_SECRET", raising=False)
        importlib.reload(auth_module)
