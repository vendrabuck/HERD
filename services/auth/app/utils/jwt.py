import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from herd_common.auth import log_unusable_key
from herd_common.jwt_codec import decode_access_token

from app.config import settings

logger = logging.getLogger(__name__)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode["exp"] = expire
    to_encode["iat"] = datetime.now(timezone.utc)
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token() -> tuple[str, str, datetime]:
    """Returns (raw_token, token_hash, expires_at)."""
    raw_token = str(uuid.uuid4())
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    return raw_token, token_hash, expires_at


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def verify_access_token(token: str) -> dict:
    """Return the payload of a token that verifies; raise InvalidTokenError otherwise.

    The shared decode (`herd_common.jwt_codec.decode_access_token`) raises
    `jwt.InvalidTokenError` for a token that does not verify and another
    `jwt.PyJWTError` (`InvalidKeyError`) when the configured key cannot be used
    for the algorithm. Both are raised here as `jwt.InvalidTokenError`, so every
    caller answers 401 rather than letting a key error become a 500; a key error
    is logged by class name first (`log_unusable_key`).
    """
    try:
        return decode_access_token(token, settings.secret_key, settings.algorithm)
    except jwt.InvalidTokenError:
        raise
    except jwt.PyJWTError as exc:
        log_unusable_key(logger, exc)
        raise jwt.InvalidTokenError("token could not be verified") from exc
