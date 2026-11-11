"""The one access-token decode every HERD service uses, and the one ID-token decode.

Every app module that verifies an access token calls `decode_access_token`
(`herd_common.auth.make_auth_dependencies`, auth's `verify_access_token`,
notifications' `get_bearer_credentials`, and the settings round trip in
`herd_common.auth_settings`); `tests/unit/test_jwt_settings_wiring.py` fails on
a PyJWT decode called anywhere else in the scanned code, except the config
service's own session decode, which may not import `herd_common`.

Two choices are made for `decode_access_token`, once, by decision (issue #1186):

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

`decode_id_token` is the second decode (ADR 0016, issue #37): an OpenID Connect
ID token, signed by the identity provider with an asymmetric key from its JWKS.
It is a separate helper, not a mode of `decode_access_token`, so neither decode
can be widened by a caller of the other. It shares the leeway constant, passes
exactly one algorithm, which must be one of `ID_TOKEN_ALGORITHMS` (never `none`,
never an HS algorithm, so a token MACed with the client secret or with the
public key bytes is refused whatever the caller passes), requires `exp`, `iat`,
`iss`, `aud`, `sub`, and `nonce`, checks `iss` for exact equality and `aud` for
membership, refuses an empty expected issuer or audience, and, unlike the
access-token decode, turns PyJWT's minimum key length ON
(`enforce_minimum_key_length`), so an RSA provider key under 2048 bits raises
`jwt.InvalidKeyError` instead of verifying with a warning. It does not
compare the nonce, check `azp`, or refuse an empty `sub`; the auth service's
`oidc_service` does, because those need the flow's state.
`tests/unit/test_jwt_settings_wiring.py` admits it as one `DIRECT_DECODE_ALLOWED`
entry.
"""

import jwt

ACCESS_TOKEN_LEEWAY_SECONDS = 10

# The ID-token algorithms HERD accepts, all asymmetric (ADR 0016, D8). The auth
# service's `OIDC_ID_TOKEN_ALGORITHM` Literal holds exactly these values, pinned
# equal by `services/auth/tests/test_oidc_settings.py`.
ID_TOKEN_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
)

# The claims an ID token must carry (a claim whose value is null counts as absent).
ID_TOKEN_REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "nonce")


def decode_access_token(token: str, key: str, algorithm: str) -> dict:
    """Verify `token` with `key` under exactly `algorithm` and return its claims.

    Raises `jwt.PyJWTError` for any token or key that does not verify.
    """
    return jwt.decode(token, key, algorithms=[algorithm], leeway=ACCESS_TOKEN_LEEWAY_SECONDS)


def decode_id_token(token: str, key, algorithm: str, *, issuer: str, audience: str) -> dict:
    """Verify an OpenID Connect ID token and return its claims.

    `key` is the provider's public key (a `jwt.PyJWK` or a `cryptography` public
    key), `algorithm` the one pinned algorithm, `issuer` the exact expected `iss`,
    and `audience` the client id that `aud` must contain.

    Raises `jwt.InvalidAlgorithmError` before any decode when `algorithm` is not
    one of `ID_TOKEN_ALGORITHMS`, `jwt.InvalidIssuerError` or
    `jwt.InvalidAudienceError` when the expected value is empty,
    `jwt.InvalidKeyError` for an RSA key under 2048 bits (PyJWT's minimum; EC
    keys have no PyJWT minimum, the curve fixes their size), and any other
    `jwt.PyJWTError` for a token or key that does not verify (PyJWT 2.15 turns
    a `RecursionError` from a deeply nested header or payload into
    `jwt.DecodeError` itself).
    """
    if algorithm not in ID_TOKEN_ALGORITHMS:
        raise jwt.InvalidAlgorithmError("The specified alg value is not allowed")
    if not issuer:
        raise jwt.InvalidIssuerError("Invalid issuer")
    if not audience:
        raise jwt.InvalidAudienceError("Invalid audience")
    return jwt.decode(
        token,
        key,
        algorithms=[algorithm],
        issuer=issuer,
        audience=audience,
        leeway=ACCESS_TOKEN_LEEWAY_SECONDS,
        options={
            "require": list(ID_TOKEN_REQUIRED_CLAIMS),
            "enforce_minimum_key_length": True,
        },
    )
