"""Startup validation of the shared access-token settings.

Every service that checks access tokens declares the two settings with the
annotated types below, so the rules live in one place and a bad value stops the
service at Settings load with a pydantic ValidationError naming the field:

    from herd_common.auth_settings import AuthSecretKey, JwtAlgorithm

    class Settings(HerdBaseSettings):
        secret_key: AuthSecretKey
        algorithm: JwtAlgorithm = "HS256"

`AuthSecretKey` refuses an empty value, a whitespace-only value, and any value
that, stripped, starts with `<` and ends with `>`: the shape of the placeholder
`.env.example` ships (`<secret key goes here>`), in any wording. A value whose
UTF-8 encoding is shorter than `MIN_SECRET_KEY_BYTES` loads, and one warning is
logged through the `logging` module (never `warnings`, which the test suites turn
into errors) carrying only the byte length, never the value or any part of it.

`JwtAlgorithm` accepts exactly HS256, HS384, or HS512 (case-sensitive).

The compose variables are `AUTH_SECRET_KEY` and `AUTH_ALGORITHM`; compose and the
config loader hand them to each service as `SECRET_KEY` and `ALGORITHM`, so the
messages name the variable an operator sets.
"""

import logging
from typing import Annotated

from pydantic import AfterValidator

logger = logging.getLogger(__name__)

MIN_SECRET_KEY_BYTES = 32

ALLOWED_JWT_ALGORITHMS = ("HS256", "HS384", "HS512")

SECRET_KEY_REFUSED_MESSAGE = (
    "AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env to a "
    "random value, for example the output of openssl rand -hex 32"
)

ALGORITHM_REFUSED_MESSAGE = "AUTH_ALGORITHM must be one of HS256, HS384, HS512"

SHORT_SECRET_KEY_LOG_MESSAGE = (
    "AUTH_SECRET_KEY is %d bytes; use at least %d bytes, for example the output of "
    "openssl rand -hex 32"
)


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def validate_auth_secret(value: str) -> str:
    """Refuse an unset, blank, or placeholder secret; warn once when it is short."""
    if not value.strip() or _is_placeholder(value):
        raise ValueError(SECRET_KEY_REFUSED_MESSAGE)
    byte_length = len(value.encode("utf-8"))
    if byte_length < MIN_SECRET_KEY_BYTES:
        logger.warning(
            SHORT_SECRET_KEY_LOG_MESSAGE,
            byte_length,
            MIN_SECRET_KEY_BYTES,
            extra={"action": "auth_secret_key_short", "byte_length": byte_length},
        )
    return value


def validate_jwt_algorithm(value: str) -> str:
    """Accept only the three HMAC algorithms the shared secret can sign with."""
    if value not in ALLOWED_JWT_ALGORITHMS:
        raise ValueError(ALGORITHM_REFUSED_MESSAGE)
    return value


AuthSecretKey = Annotated[str, AfterValidator(validate_auth_secret)]
JwtAlgorithm = Annotated[str, AfterValidator(validate_jwt_algorithm)]
