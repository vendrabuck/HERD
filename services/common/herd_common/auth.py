"""
Shared JWT verification and role enforcement factory for downstream services.

Usage in a service:
    from herd_common.auth import make_auth_dependencies
    get_current_user_payload, require_admin = make_auth_dependencies(
        secret_key=settings.secret_key,
        algorithm=settings.algorithm,
    )

`caller_id` consumes the same JWT payload shape that factory produces, to turn
the payload's `sub` claim into a UUID:
    from herd_common.auth import caller_id
    user_id = caller_id(payload)

`log_unusable_key` is shared by the three token decode sites (this module's
dependency, auth's `verify_access_token`, notifications' `get_bearer_credentials`),
each of which decodes through `herd_common.jwt_codec.decode_access_token`: each
answers 401 for any `jwt.PyJWTError`, and one that is not a `jwt.InvalidTokenError`
is a key PyJWT could not use, a server-side setting rather than a bad token, so it
is logged once at ERROR with the exception class name only.
"""

import logging
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from herd_common.jwt_codec import decode_access_token

logger = logging.getLogger(__name__)

ADMIN_ROLES = {"admin", "superadmin"}

KEY_UNUSABLE_LOG_MESSAGE = "access token not checked: the configured key is unusable (%s)"


def log_unusable_key(log: logging.Logger, exc: jwt.PyJWTError) -> None:
    """Log a key error raised while verifying a token, by class name only.

    A `jwt.InvalidTokenError` (and its subclasses) is a token that did not verify
    and is not logged; any other `jwt.PyJWTError` (`InvalidKeyError`, raised while
    PyJWT prepares the key) is logged at ERROR with action `jwt_key_unusable` and
    the class name. The exception text is never logged.
    """
    if isinstance(exc, jwt.InvalidTokenError):
        return
    error_class = type(exc).__name__
    log.error(
        KEY_UNUSABLE_LOG_MESSAGE,
        error_class,
        extra={"action": "jwt_key_unusable", "error_class": error_class},
    )


bearer_scheme = HTTPBearer()


def caller_id(payload: dict) -> uuid.UUID:
    """Extract and parse the caller's user id from a decoded JWT payload's `sub`.

    Raises 401 "Invalid subject in token" when `sub` is missing, not a string,
    or not a valid UUID.
    """
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid subject in token",
        )


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
            payload = decode_access_token(credentials.credentials, secret_key, algorithm)
            if not payload.get("sub"):
                raise credentials_exception
            return payload
        except jwt.PyJWTError as exc:
            # PyJWTError is the base of InvalidTokenError and of InvalidKeyError,
            # which PyJWT raises while preparing the verification key, so a token
            # or a key that cannot be verified answers the same 401; a key error
            # is also logged.
            log_unusable_key(logger, exc)
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
