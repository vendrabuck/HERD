import json
import logging
import os
import secrets

import bcrypt

from app.config_schema import CONFIG_SCHEMA, REQUIRED_KEYS

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("HERD_CONFIG_DATA_DIR", "/data/herd-config")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
AUTH_FILE = os.path.join(DATA_DIR, "config_auth.json")


class ConfigAuthError(Exception):
    """Raised when the config auth file exists but cannot be read/parsed."""


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _bootstrap_marker_file() -> str:
    # Derived at call time so the test fixture's DATA_DIR monkeypatch applies.
    return os.path.join(DATA_DIR, "config.bootstrapped")


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# -- Auth file --


def _initial_password() -> tuple[str, bool]:
    """Resolve the config-page password seeded on first boot.

    Returns (password, changed). An operator-provided CONFIG_ADMIN_PASSWORD is
    treated as a chosen credential (changed=True, so the write surface is
    unlocked immediately). Otherwise a random one-time password is generated and
    logged ONCE at WARNING for the operator to read from the container logs, and
    marked unrotated (changed=False) so the write/apply surface stays locked
    until it is changed. A guessable source-visible constant is never used: the
    previous "admin123!" default let anyone reach the publicly-routed config
    write surface (issue #256).
    """
    env_pw = os.environ.get("CONFIG_ADMIN_PASSWORD", "").strip()
    if env_pw:
        return env_pw, True
    generated = secrets.token_urlsafe(24)
    logger.warning(
        "No CONFIG_ADMIN_PASSWORD set; generated a one-time config-page password: %s "
        "Log in with it and change it; the config write surface is locked until you do.",
        generated,
    )
    return generated, False


def _default_auth() -> dict:
    password, changed = _initial_password()
    return {
        "password_hash": _hash_password(password),
        "password_changed": changed,
    }


def load_auth() -> dict:
    _ensure_data_dir()
    if not os.path.exists(AUTH_FILE):
        auth = _default_auth()
        _save_auth(auth)
        return auth
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # Fail closed: do NOT regenerate the default here, that would silently
        # reset the admin password. Recovery is operator-driven: remove the
        # corrupt file and the missing-file branch above regenerates the default.
        logger.error("Corrupt config auth file %s: %s", AUTH_FILE, exc)
        raise ConfigAuthError(f"corrupt auth file {AUTH_FILE}") from exc


def _save_auth(auth: dict) -> None:
    _ensure_data_dir()
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f, indent=2)


def verify_password(password: str) -> bool:
    try:
        auth = load_auth()
    except ConfigAuthError:
        # Fail closed: deny login when the auth file is present but corrupt.
        return False
    return _check_password(password, auth["password_hash"])


def change_password(new_password: str) -> None:
    # Unreachable with a corrupt auth file: this is only called behind
    # require_config_session, which requires a successful login, and
    # verify_password now denies login when the auth file is corrupt.
    auth = load_auth()
    auth["password_hash"] = _hash_password(new_password)
    auth["password_changed"] = True
    _save_auth(auth)


def is_password_changed() -> bool:
    try:
        return load_auth().get("password_changed", False)
    except ConfigAuthError:
        # Keep the public /status endpoint up; report the safe default.
        return False


# -- Config file --


def is_configured() -> bool:
    return os.path.exists(CONFIG_FILE)


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Ignoring corrupt config file %s: %s", CONFIG_FILE, exc)
        return {}


def save_config(values: dict) -> list[str]:
    """Save config values. Returns list of validation errors (empty on success)."""
    errors = []
    for key in REQUIRED_KEYS:
        val = values.get(key)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            errors.append(f"{key} is required")
    if errors:
        return errors

    _ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(values, f, indent=2)
    # A save is a deliberate operator decision: dropping the bootstrap marker
    # promotes config.json above the environment in every service from its
    # next start (see herd_common.config_loader.herd_settings_sources).
    try:
        os.remove(_bootstrap_marker_file())
    except FileNotFoundError:
        pass
    return []


def load_env_values() -> dict[str, str]:
    """Return CONFIG_SCHEMA keys that are present and non-blank in os.environ."""
    values: dict[str, str] = {}
    for field in CONFIG_SCHEMA:
        raw = os.environ.get(field["key"], "")
        if raw.strip():
            values[field["key"]] = raw
    return values


def bootstrap_from_env() -> bool:
    """Write config.json from process env on first startup when possible.

    Idempotent: returns False if config.json already exists. Reads every
    CONFIG_SCHEMA key from os.environ and writes a config file when every
    REQUIRED_KEYS entry is present and non-empty. When required values are
    missing the user still reaches the config UI as before.

    The admin password file is left untouched so the config page keeps its
    default-password gate on first visit.
    """
    if os.path.exists(CONFIG_FILE):
        return False

    values = load_env_values()

    missing = [key for key in REQUIRED_KEYS if not values.get(key, "").strip()]
    if missing:
        logger.warning(
            "Config auto-bootstrap skipped; missing required env vars: %s",
            ", ".join(missing),
        )
        return False

    _ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(values, f, indent=2)
    # Mark the file as env-bootstrapped: it is a copy of the environment, not
    # an operator decision, so services keep treating the environment as
    # authoritative until the first real config-UI save deletes this marker.
    with open(_bootstrap_marker_file(), "w") as f:
        f.write("created by first-boot auto-bootstrap; deleted on first config-UI save\n")
    logger.info("Config bootstrapped from environment into %s", CONFIG_FILE)
    return True
