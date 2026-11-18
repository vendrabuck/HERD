"""Shared config loader for reading settings from the HERD config JSON file.

All services mount the herd-config volume at /etc/herd/ (read-only).
The config service writes config.json there. This module provides
a custom Pydantic settings source that reads from that file, mapping
the user-facing config keys to service-specific env var names.
"""

import json
import logging
import os
import stat
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

    A missing path (nothing there, not even a link) is not an error and logs
    nothing (OPS-SET-3). Anything else that is not a readable JSON object is
    ignored with one WARNING naming the path and the reason, never the content,
    so environment values and model defaults apply. MemoryError on a huge file is
    not handled.
    """
    if not os.path.lexists(CONFIG_FILE):
        return {}
    loaded, reason = _read_config_object(CONFIG_FILE)
    if reason is not None:
        logger.warning("Ignoring unusable HERD config file %s (%s)", CONFIG_FILE, reason)
        return {}
    return loaded


def _read_config_object(path: str) -> tuple[dict, None] | tuple[None, str]:
    """(object, None) for a regular file holding a JSON object, else (None, reason).

    The reason is an exception's class name, `not a regular file: <kind>`, or
    `not a JSON object: <type>`; it never quotes the content. The path is checked
    with stat (following links) before it is opened, so a FIFO or a device is
    refused without a read that could block import forever, and a dangling or
    looping link is refused by its OSError. The open is non-blocking and the
    descriptor is checked again, so a path swapped after the stat is refused too.
    """
    try:
        mode = os.stat(path).st_mode
        if not stat.S_ISREG(mode):
            return None, f"not a regular file: {_file_kind(mode)}"
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        # An explicit encoding: the answer must not depend on the container's locale.
        with open(fd, encoding="utf-8") as f:
            mode = os.fstat(f.fileno()).st_mode
            if not stat.S_ISREG(mode):
                return None, f"not a regular file: {_file_kind(mode)}"
            loaded = json.load(f)
    except (ValueError, RecursionError, OSError) as exc:
        # ValueError covers JSONDecodeError (an empty or truncated file included),
        # a byte that is not UTF-8 (UnicodeDecodeError), and an integer over
        # Python's digit limit; RecursionError is nesting deeper than the parser
        # allows; OSError is an unreadable file or a dangling or looping link.
        return None, type(exc).__name__
    if not isinstance(loaded, dict):
        # Valid JSON that is not an object (a list, a string, a number, true,
        # null) holds no settings; named by its type only, never its value.
        return None, f"not a JSON object: {type(loaded).__name__}"
    return loaded, None


def _file_kind(mode: int) -> str:
    for test, kind in (
        (stat.S_ISDIR, "directory"),
        (stat.S_ISFIFO, "FIFO"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISBLK, "block device"),
    ):
        if test(mode):
            return kind
    return "other"


def _build_database_url(data: dict) -> str | None:
    parts = {key: data.get(key) for key in _DATABASE_URL_KEYS}
    if any(_is_unset(value) for value in parts.values()):
        # A missing, null, empty, or whitespace-only part leaves the URL underived
        # (OPS-SET-4: such a string means unset; OPS-SET-5).
        return None
    not_usable = [key for key, value in parts.items() if _url_part(value) is None]
    if not_usable:
        # A boolean, list, or object part leaves it underived too, and so does a
        # string holding a lone surrogate: URL.create raised TypeError on a
        # non-string username or database, the rendering raised UnicodeEncodeError
        # on a lone surrogate, either of which stopped the service at import, and
        # a list, object, or boolean password was stringified into a wrong URL.
        # The log names the keys, never the values.
        logger.warning(
            "Not deriving DATABASE_URL from HERD config file %s: %s must be text or a number",
            CONFIG_FILE,
            ", ".join(not_usable),
        )
        return None
    user, password, db = (_url_part(value) for value in parts.values())
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


def _is_unset(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _url_part(value: object) -> str | None:
    """The text a URL part is built from, or None when the value cannot be one.

    A string that encodes as UTF-8 is used as is. An int or a float is used as
    its Python text (`12345`; a float as Python writes it, so `1e3` is `1000.0`),
    which is what the URL held before #1451 for a numeric password. A boolean (an
    int to Python), a list, an object, and a lone surrogate are refused.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


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
