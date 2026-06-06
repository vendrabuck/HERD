"""Shared config loader for reading settings from the HERD config JSON file.

All services mount the herd-config volume at /etc/herd/ (read-only).
The config service writes config.json there. This module provides
a custom Pydantic settings source that reads from that file, mapping
the user-facing config keys to service-specific env var names.
"""

import json
import os
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

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


def _load_json() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _build_database_url(data: dict) -> str | None:
    user = data.get("POSTGRES_USER")
    password = data.get("POSTGRES_PASSWORD")
    db = data.get("POSTGRES_DB")
    if user and password and db:
        return f"postgresql+asyncpg://{user}:{password}@postgres:5432/{db}"
    return None


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
            mapped_key = _KEY_MAP.get(key, key)
            self._data[mapped_key.lower()] = value
        # Compute DATABASE_URL from POSTGRES_* fields
        db_url = _build_database_url(raw)
        if db_url:
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
