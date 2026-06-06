from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Internal-only signing key for config session tokens.
# This is not sensitive: the config service is standalone and these tokens
# only grant access to config endpoints, not to HERD user data.
_CONFIG_SESSION_SECRET = "herd-config-session-internal-key"
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
