"""Shared config loader for reading settings from the HERD config JSON file.

All services mount the herd-config volume at /etc/herd/ (read-only).
The config service writes config.json there. This module provides
a custom Pydantic settings source that reads from that file, mapping
the user-facing config keys to service-specific env var names.
"""

import json
import logging
import os
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy.engine import URL

logger = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get("HERD_CONFIG_FILE", "/etc/herd/config.json")

# Mapping from config.json keys to the env var names services expect.
# docker-compose.yml performs this mapping via its environment block;
# the config loader replicates it so services work with or without
# docker-compose env var passthrough.
_KEY_MAP = {
    "AUTH_SECRET_KEY": "SECRET_KEY",
    "AUTH_ALGORITHM": "ALGORITHM",
    "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES": "ACCESS_TOKEN_EXPIRE_MINUTES",
    "AUTH_REFRESH_TOKEN_EXPIRE_DAYS": "REFRESH_TOKEN_EXPIRE_DAYS",
}

# The file keys a derived DATABASE_URL is built from (OPS-SET-5).
_DATABASE_URL_KEYS = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def _load_json() -> dict:
    """The parsed config.json object, or {} when there is none to use (OPS-SET-4).

    No content of the file may stop a service from starting: every service but
    config builds its settings from this at import. A file that is not a
    readable JSON object is ignored with one WARNING naming the failure's class
    (never the content), so environment values and model defaults apply,
    exactly as for a missing file. MemoryError on a huge file is not handled.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        # An explicit encoding: the answer must not depend on the container's locale.
        with open(CONFIG_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
    except (ValueError, RecursionError, OSError) as exc:
        # ValueError covers JSONDecodeError (an empty or truncated file included),
        # a byte that is not UTF-8 (UnicodeDecodeError), and an integer over
        # Python's digit limit; RecursionError is nesting deeper than the parser
        # allows; OSError is an unreadable path, a directory included. The same
        # tuple the config service's own loader catches (config_store.load_config).
        logger.warning("Ignoring corrupt HERD config file %s (%s)", CONFIG_FILE, type(exc).__name__)
        return {}
    if not isinstance(loaded, dict):
        # Valid JSON that is not an object (a list, a string, a number, true,
        # null) holds no settings; treat it like a corrupt file, naming the
        # class only, never the value.
        logger.warning(
            "Ignoring HERD config file %s: not a JSON object (%s)",
            CONFIG_FILE,
            type(loaded).__name__,
        )
        return {}
    return loaded


def _build_database_url(data: dict) -> str | None:
    parts = {key: data.get(key) for key in _DATABASE_URL_KEYS}
    if not all(parts.values()):
        # A missing or empty part leaves the URL underived (OPS-SET-5).
        return None
    not_text = [key for key, value in parts.items() if not _is_url_text(value)]
    if not_text:
        # A part that is not text leaves it underived too: URL.create raises
        # TypeError on a non-string username or database and the rendering raises
        # UnicodeEncodeError on a lone surrogate, either of which would stop the
        # service at import, and a list or a boolean password would be stringified
        # into a wrong URL. The log names the keys, never the values.
        logger.warning(
            "Not deriving DATABASE_URL from HERD config file %s: %s must be text",
            CONFIG_FILE,
            ", ".join(not_text),
        )
        return None
    user, password, db = parts.values()
    # Use URL.create so each component is percent-encoded; a raw f-string
    # breaks on passwords containing @ : / # ? [ ] (issue #209).
    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host="postgres",
        port=5432,
        database=db,
    )
    return url.render_as_string(hide_password=False)


def _is_url_text(value: object) -> bool:
    """True for a string that encodes as UTF-8 (no lone surrogate)."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_configured() -> bool:
    return os.path.exists(CONFIG_FILE)


class HerdJsonConfigSource(PydanticBaseSettingsSource):
    """Pydantic settings source backed by the HERD config JSON file."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        raw = _load_json()
        # Build mapped dict: apply key mapping and compute derived values
        self._data: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, str) and value.strip() == "":
                # An empty string in config.json means "unset": the key falls
                # through to the environment instead of shadowing it.
                continue
            mapped_key = _KEY_MAP.get(key, key)
            self._data[mapped_key.lower()] = value
        # Compute DATABASE_URL from POSTGRES_* fields. The derived URL
        # hardcodes the in-compose postgres:5432 host, so it is a fallback
        # only: an explicitly set DATABASE_URL env var (external DB, pooler,
        # TLS params) must outrank it in both source orders below.
        db_url = _build_database_url(raw)
        if db_url and not os.environ.get("DATABASE_URL"):
            self._data["database_url"] = db_url

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        val = self._data.get(field_name)
        return val, field_name, val is not None

    def __call__(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            val, _, is_set = self.get_field_value(field_info, field_name)
            if is_set:
                d[field_name] = val
        return d


BOOTSTRAP_MARKER = "config.bootstrapped"


def _bootstrap_marker_file() -> str:
    # Derived at call time from CONFIG_FILE so test reloads with a patched
    # HERD_CONFIG_FILE resolve the marker beside the patched config path.
    return os.path.join(os.path.dirname(CONFIG_FILE), BOOTSTRAP_MARKER)


def herd_settings_sources(
    settings_cls: type[BaseSettings],
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
    file_secret_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    """Canonical settings-source order for every HERD service.

    Earlier sources win. The config file outranks the environment only once an
    operator has actually saved through the config UI: compose injects the
    same keys as container env vars, and a container's env is fixed at
    creation, so with env-first ordering a config-UI save could never change a
    running deployment.

    A config.json created solely by first-boot auto-bootstrap is a copy of the
    environment, not an operator decision; the config service marks it with a
    sibling config.bootstrapped file and deletes that marker on the first real
    UI save. While the marker exists the file ranks below the environment, so
    a stack driven purely by .env behaves exactly as it did before the file
    existed. Keys absent from the file always resolve from env and dotenv, a
    host with no config.json (unit tests, bare processes) behaves as pure env,
    and init_settings stays first so explicit constructor arguments override
    everything.
    """
    file_source = HerdJsonConfigSource(settings_cls)
    if os.path.exists(_bootstrap_marker_file()):
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_source,
            file_secret_settings,
        )
    return (
        init_settings,
        file_source,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    )
