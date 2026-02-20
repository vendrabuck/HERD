"""
JWT verification and role enforcement for the Inventory service.
Each service verifies tokens locally using the shared secret; no round-trip to the auth service.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

bearer_scheme = HTTPBearer()

ADMIN_ROLES = {"admin", "superadmin"}


def get_current_user_payload(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Verify JWT and return the decoded payload. Any authenticated user passes."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            credentials.credentials, settings.secret_key, algorithms=[settings.algorithm]
        )
        if payload.get("sub") is None:
            raise credentials_exception
        return payload
    except JWTError:
        raise credentials_exception


def require_admin(
    payload: dict = Depends(get_current_user_payload),
) -> dict:
    """Require the caller to hold the 'admin' or 'superadmin' role."""
    if payload.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or superadmin role required",
        )
    return payload
