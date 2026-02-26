"""
Shared JWT verification and role enforcement factory for downstream services.

Usage in a service:
    from herd_common.auth import make_auth_dependencies
    get_current_user_payload, require_admin = make_auth_dependencies(
        secret_key=settings.secret_key,
        algorithm=settings.algorithm,
    )
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

ADMIN_ROLES = {"admin", "superadmin"}

bearer_scheme = HTTPBearer()


def make_auth_dependencies(secret_key: str, algorithm: str = "HS256"):
    """Return (get_current_user_payload, require_admin) dependency callables."""

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
            payload = jwt.decode(credentials.credentials, secret_key, algorithms=[algorithm])
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

    return get_current_user_payload, require_admin
