"""Tests for herd_common.base_settings.HerdBaseSettings.

Pins that a service Settings(HerdBaseSettings) subclass resolves through the
exact same source order every service previously assembled by hand via its
own settings_customise_sources classmethod: file-above-env in the normal
case, env-above-file while config.bootstrapped exists, and the DATABASE_URL
env guard. These tests, together with test_config_loader.py's existing
herd_settings_sources coverage (which they deliberately duplicate at the
class-inheritance layer rather than the raw-source-tuple layer), are the
proof that the issue #384 migration to HerdBaseSettings changed no resolved
value.
"""

import importlib
import json


def _reload_base_settings(monkeypatch, config_path: str):
    monkeypatch.setenv("HERD_CONFIG_FILE", config_path)
    from herd_common import base_settings, config_loader

    importlib.reload(config_loader)
    return importlib.reload(base_settings)


def _representative_settings_cls(base_settings_module):
    """Stand-in for a real service's `class Settings(HerdBaseSettings)`."""

    class Settings(base_settings_module.HerdBaseSettings):
        database_url: str = "sqlite+aiosqlite:///:memory:"
        cors_origins: str = "default-origins"
        log_level: str = "INFO"

    return Settings


def test_file_beats_env_without_bootstrap_marker(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    base_settings = _reload_base_settings(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _representative_settings_cls(base_settings)()
    assert settings.cors_origins == "https://from-file"


def test_env_beats_file_while_bootstrap_marker_present(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    (tmp_path / "config.bootstrapped").write_text("marker")
    base_settings = _reload_base_settings(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _representative_settings_cls(base_settings)()
    assert settings.cors_origins == "https://from-env"


def test_env_database_url_beats_file_derived_database_url(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"POSTGRES_USER": "herd", "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "herd"})
    )
    base_settings = _reload_base_settings(monkeypatch, str(config_file))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com:6432/herd")

    settings = _representative_settings_cls(base_settings)()
    assert settings.database_url == "postgresql+asyncpg://u:p@db.example.com:6432/herd"


def test_pure_env_when_config_file_missing(tmp_path, monkeypatch):
    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "missing.json"))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _representative_settings_cls(base_settings)()
    assert settings.cors_origins == "https://from-env"


def test_model_config_env_file_and_case_sensitivity():
    from herd_common.base_settings import HerdBaseSettings

    assert HerdBaseSettings.model_config["env_file"] == ".env"
    assert HerdBaseSettings.model_config["case_sensitive"] is False


def test_subclass_inherits_settings_customise_sources_without_override():
    from herd_common.base_settings import HerdBaseSettings

    class Settings(HerdBaseSettings):
        cors_origins: str = "default"

    assert Settings.settings_customise_sources.__func__ is (
        HerdBaseSettings.settings_customise_sources.__func__
    )


# --- Database pool settings (issue #1352) ------------------------------------------


def test_pool_settings_default_to_five_ten_and_pre_ping(tmp_path, monkeypatch):
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_PRE_PING"):
        monkeypatch.delenv(name, raising=False)
    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "absent.json"))
    settings = _representative_settings_cls(base_settings)()
    assert (settings.db_pool_size, settings.db_max_overflow, settings.db_pool_pre_ping) == (
        5,
        10,
        True,
    )


def test_pool_settings_read_their_env_vars(tmp_path, monkeypatch):
    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "absent.json"))
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_PRE_PING", "false")
    settings = _representative_settings_cls(base_settings)()
    assert (settings.db_pool_size, settings.db_max_overflow, settings.db_pool_pre_ping) == (
        3,
        0,
        False,
    )


def test_pool_settings_refuse_an_empty_pool_or_negative_overflow(tmp_path, monkeypatch):
    import pydantic
    import pytest

    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "absent.json"))
    settings_cls = _representative_settings_cls(base_settings)
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    with pytest.raises(pydantic.ValidationError, match="db_pool_size"):
        settings_cls()
    monkeypatch.setenv("DB_POOL_SIZE", "5")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "-1")
    with pytest.raises(pydantic.ValidationError, match="db_max_overflow"):
        settings_cls()


# --- Request body limit (issue #1466) ---------------------------------------------


def test_the_body_limit_defaults_to_four_mebibytes(tmp_path, monkeypatch):
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "absent.json"))
    assert _representative_settings_cls(base_settings)().max_request_body_bytes == 4194304


def test_the_body_limit_reads_decimal_digits_from_env_and_numbers_from_the_file(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"MAX_REQUEST_BODY_BYTES": 131072}))
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    base_settings = _reload_base_settings(monkeypatch, str(config))
    assert _representative_settings_cls(base_settings)().max_request_body_bytes == 131072
    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "absent.json"))
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", " 8388608 ")
    assert _representative_settings_cls(base_settings)().max_request_body_bytes == 8388608


def test_the_body_limit_refuses_what_config_also_refuses(tmp_path, monkeypatch):
    """The same reading as config's `app/request_limits.py`: decimal digits only, at
    least 65536. A service refuses to start on anything else, naming the rule and
    never the value."""
    import pydantic
    import pytest

    base_settings = _reload_base_settings(monkeypatch, str(tmp_path / "absent.json"))
    settings_cls = _representative_settings_cls(base_settings)
    for raw in ("8388608.0", "1_000_000", "1e6", "+65536", "65535", "0", "-1", "4MB"):
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", raw)
        with pytest.raises(pydantic.ValidationError, match="decimal digits") as refused:
            settings_cls()
        if len(raw) > 3:  # a short value could match unrelated digits in the message
            assert raw not in str(refused.value)
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "65536")
    assert settings_cls().max_request_body_bytes == 65536
