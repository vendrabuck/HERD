import json
import logging
import os

import bcrypt

from app.config_schema import CONFIG_SCHEMA, REQUIRED_KEYS

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("HERD_CONFIG_DATA_DIR", "/data/herd-config")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
AUTH_FILE = os.path.join(DATA_DIR, "config_auth.json")

DEFAULT_PASSWORD = "admin123!"


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# -- Auth file --


def _default_auth() -> dict:
    return {
        "password_hash": _hash_password(DEFAULT_PASSWORD),
        "password_changed": False,
    }


def load_auth() -> dict:
    _ensure_data_dir()
    if not os.path.exists(AUTH_FILE):
        auth = _default_auth()
        _save_auth(auth)
        return auth
    with open(AUTH_FILE) as f:
        return json.load(f)


def _save_auth(auth: dict) -> None:
    _ensure_data_dir()
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f, indent=2)


def verify_password(password: str) -> bool:
    auth = load_auth()
    return _check_password(password, auth["password_hash"])


def change_password(new_password: str) -> None:
    auth = load_auth()
    auth["password_hash"] = _hash_password(new_password)
    auth["password_changed"] = True
    _save_auth(auth)


def is_password_changed() -> bool:
    return load_auth().get("password_changed", False)


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
    logger.info("Config bootstrapped from environment into %s", CONFIG_FILE)
    return True
