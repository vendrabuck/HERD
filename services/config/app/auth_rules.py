"""The access-token setting rules, applied before the config service writes them.

Every other service validates AUTH_SECRET_KEY and AUTH_ALGORITHM when its
Settings load, through herd_common.auth_settings. The config service cannot
import that module: it is deliberately standalone (no herd-common dependency in
its pyproject.toml, nothing copied into its image), so an import of herd_common
here fails at boot with ModuleNotFoundError. This module is a standard-library
copy of two of those rules, so the config UI and the first-boot bootstrap never
write a value every service would then refuse at startup:

- AUTH_SECRET_KEY must have a printable character other than white space, and,
  stripped, must not start with `<` and end with `>` (the `.env.example`
  placeholder shape);
- AUTH_ALGORITHM, when set to anything but an empty string (which the services'
  config loader reads as unset), must be HS256, HS384, or HS512.

The two message strings are the shared module's, spelled identically;
tests/test_auth_rules.py pins them equal to herd_common.auth_settings on the
host, where both are importable. The services' last two checks, the key-shape
check and a signing round trip through PyJWT and herd_common.jwt_codec, are not
copied (this service ships PyJWT for its own session but not herd_common): a
value that passes here and fails either is refused at service startup with
herd_common.auth_settings.SECRET_KEY_UNUSABLE_MESSAGE, which names the config UI
as well as .env.
"""

ALLOWED_JWT_ALGORITHMS = ("HS256", "HS384", "HS512")

SECRET_KEY_REFUSED_MESSAGE = (
    "AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env, or in "
    "the config UI if a configuration was saved there, to a random value, for "
    "example the output of openssl rand -hex 32"
)

ALGORITHM_REFUSED_MESSAGE = "AUTH_ALGORITHM must be one of HS256, HS384, HS512"


def secret_key_refused(value: object) -> bool:
    """True for a missing, non-string, blank, or placeholder secret."""
    if not isinstance(value, str):
        return True
    if not any(ch.isprintable() and not ch.isspace() for ch in value):
        return True
    stripped = value.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def algorithm_refused(value: object) -> bool:
    """True for a set algorithm outside the three HMAC algorithms."""
    if value is None or value == "":
        return False
    return value not in ALLOWED_JWT_ALGORITHMS


def auth_setting_errors(values: dict) -> list[str]:
    """The refusal messages for the access-token settings in `values`, if any."""
    errors = []
    if secret_key_refused(values.get("AUTH_SECRET_KEY")):
        errors.append(SECRET_KEY_REFUSED_MESSAGE)
    if algorithm_refused(values.get("AUTH_ALGORITHM")):
        errors.append(ALGORITHM_REFUSED_MESSAGE)
    return errors
