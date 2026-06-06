import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt

from app.config import settings


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
    """Raises JWTError if invalid. Returns payload dict."""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
