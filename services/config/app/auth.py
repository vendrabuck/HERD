import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def _load_session_secret() -> str:
    """Resolve the config-session signing key.

    An operator MAY pin the secret across replicas by setting
    CONFIG_SESSION_SECRET in the environment. Otherwise a random per-process
    secret is generated at import time. A hardcoded fallback is deliberately
    not shipped: the previous constant let anyone forge a valid session from
    the source-visible value and reach the config write surface. A generated
    secret does not survive a restart, which matches the intended security
    property that config sessions are ephemeral.
    """
    env_secret = os.environ.get("CONFIG_SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret
    return secrets.token_urlsafe(32)


# Signing key for config session tokens. Never a source-visible constant.
_CONFIG_SESSION_SECRET = _load_session_secret()
_ALGORITHM = "HS256"
_EXPIRE_MINUTES = 30

_bearer = HTTPBearer()


def create_session_token() -> str:
    payload = {
        "sub": "config-admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _CONFIG_SESSION_SECRET, algorithm=_ALGORITHM)


def _verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, _CONFIG_SESSION_SECRET, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token")


async def require_config_session(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    return _verify_token(credentials.credentials)
