"""Startup validation of the shared access-token settings.

Every service that checks access tokens declares the two settings with the
annotated types below, so the rules live in one place and a bad value stops the
service at Settings load with a pydantic ValidationError naming the field:

    from herd_common.auth_settings import AuthSecretKey, JwtAlgorithm

    class Settings(HerdBaseSettings):
        secret_key: AuthSecretKey
        algorithm: JwtAlgorithm = "HS256"

`AuthSecretKey` refuses, with `SECRET_KEY_REFUSED_MESSAGE`, a value with no
printable character other than white space (empty, white space only, or only
invisible format characters such as a zero-width space or a byte-order mark),
and any value that, stripped, starts with `<` and ends with `>`: the shape of
the placeholder `.env.example` ships (`<secret key goes here>`), in any wording.
It then signs one HS256 token with the value through PyJWT and verifies it with
the value through `herd_common.jwt_codec.decode_access_token`, and refuses with
`SECRET_KEY_UNUSABLE_MESSAGE` when PyJWT raises any `jwt.PyJWTError`: PyJWT's
HMAC key preparation refuses a PEM block (a public or private key, a
certificate), an ssh public key, and a JSON value carrying a `kty` member (a JSON
Web Key) with `InvalidKeyError`. Any other JSON-shaped value is an ordinary HMAC
secret to PyJWT and loads. The round trip neither logs nor returns the value or
any part of it, and it ignores PyJWT's `InsecureKeyLengthWarning` for its own two
calls only: the short-key rule is the byte-length check below, which logs, and
the test suites turn every warning into an error. A value whose UTF-8 encoding is shorter than
`MIN_SECRET_KEY_BYTES` loads, and one warning is logged through the `logging`
module (never `warnings`, which the test suites turn into errors) carrying only
the byte length.

That warning fires when a service's module-level `settings = Settings()` runs,
at import time and before the service's `setup_logging`, so it reaches the
container log as a plain-text line through Python's last-resort handler; the
`action` and `byte_length` extras are on the record but are not rendered there.

`JwtAlgorithm` accepts exactly HS256, HS384, or HS512 (case-sensitive).

The compose variables are `AUTH_SECRET_KEY` and `AUTH_ALGORITHM`; compose and the
config loader hand them to each service as `SECRET_KEY` and `ALGORITHM`, so the
messages name the variable an operator sets. A configuration saved in the config
UI outranks the environment (`herd_common.config_loader`), so the messages name
both places. The config service, which has no `herd_common` dependency, keeps a
standard-library copy of the blank, placeholder, and algorithm rules with the
same two message strings in `services/config/app/auth_rules.py`; its tests pin
the copies equal to these.
"""

import logging
import warnings
from typing import Annotated

import jwt
from jwt.warnings import InsecureKeyLengthWarning
from pydantic import AfterValidator

from herd_common.jwt_codec import decode_access_token

logger = logging.getLogger(__name__)

MIN_SECRET_KEY_BYTES = 32

ALLOWED_JWT_ALGORITHMS = ("HS256", "HS384", "HS512")

SECRET_KEY_REFUSED_MESSAGE = (
    "AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env, or in "
    "the config UI if a configuration was saved there, to a random value, for "
    "example the output of openssl rand -hex 32"
)

SECRET_KEY_UNUSABLE_MESSAGE = (
    "AUTH_SECRET_KEY cannot be used as an HMAC secret; set it in .env, or in the "
    "config UI if a configuration was saved there, to a random value, for example "
    "the output of openssl rand -hex 32"
)

ALGORITHM_REFUSED_MESSAGE = "AUTH_ALGORITHM must be one of HS256, HS384, HS512"

SHORT_SECRET_KEY_LOG_MESSAGE = (
    "AUTH_SECRET_KEY is %d bytes; use at least %d bytes, for example the output of "
    "openssl rand -hex 32"
)


def _is_blank(value: str) -> bool:
    return not any(ch.isprintable() and not ch.isspace() for ch in value)


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def _signs_and_verifies(value: str) -> bool:
    """Whether PyJWT can sign and verify one HS256 token with `value` as the key.

    PyJWT warns with `InsecureKeyLengthWarning` on both calls when the key is
    shorter than 32 bytes. That warning, and only that one, is ignored inside
    this round trip: `validate_auth_secret` enforces the short-key rule itself
    with a byte-length check and one log line, and under the test suites'
    `filterwarnings = ["error"]` the warning would otherwise refuse a short key
    the rule says must load.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureKeyLengthWarning)
            token = jwt.encode({"sub": "x"}, value, algorithm="HS256")
            decode_access_token(token, value, "HS256")
    except jwt.PyJWTError:
        return False
    return True


def validate_auth_secret(value: str) -> str:
    """Refuse a blank, placeholder, or unusable secret; warn once when it is short."""
    if _is_blank(value) or _is_placeholder(value):
        raise ValueError(SECRET_KEY_REFUSED_MESSAGE)
    if not _signs_and_verifies(value):
        raise ValueError(SECRET_KEY_UNUSABLE_MESSAGE)
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
