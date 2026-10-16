"""The one access-token decode every HERD service uses.

Every app module that verifies an access token calls `decode_access_token`
(`herd_common.auth.make_auth_dependencies`, auth's `verify_access_token`,
notifications' `get_bearer_credentials`, and the settings round trip in
`herd_common.auth_settings`); `tests/unit/test_jwt_settings_wiring.py` fails on
a PyJWT decode called anywhere else in the scanned code, except the config
service's own session decode, which may not import `herd_common`.

Two choices are made here, once, by decision (issue #1186):

- `leeway` is `ACCESS_TOKEN_LEEWAY_SECONDS` (10 s). PyJWT applies it to `exp`,
  `nbf`, and `iat`: a token is accepted up to 10 s after its `exp`, and an `iat`
  or `nbf` up to 10 s in the future, so a clock skew between the auth container
  that signs and a service that verifies does not refuse fresh tokens. PyJWT
  refuses an `iat` later than now (the library HERD used before it did not),
  which is why the skew matters.
- `enforce_minimum_key_length` is left at PyJWT's default, off. A key shorter
  than the hash's digest size then makes PyJWT emit `InsecureKeyLengthWarning`
  through `warnings` and verify the token anyway. The short-key rule is enforced
  at Settings load by `herd_common.auth_settings.validate_auth_secret`, which
  logs the byte length; enforcing it here would refuse every token on a
  deployment that works today. The warning is not suppressed here.

Failures are not wrapped: every PyJWT failure propagates as a `jwt.PyJWTError`.
`jwt.InvalidTokenError` (and its subclasses) is a token that did not verify;
any other `jwt.PyJWTError` (`InvalidKeyError`) is a configured key PyJWT cannot
use, which callers log through `herd_common.auth.log_unusable_key`.
"""

import jwt

ACCESS_TOKEN_LEEWAY_SECONDS = 10


def decode_access_token(token: str, key: str, algorithm: str) -> dict:
    """Verify `token` with `key` under exactly `algorithm` and return its claims.

    Raises `jwt.PyJWTError` for any token or key that does not verify.
    """
    return jwt.decode(token, key, algorithms=[algorithm], leeway=ACCESS_TOKEN_LEEWAY_SECONDS)
