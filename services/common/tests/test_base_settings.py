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
