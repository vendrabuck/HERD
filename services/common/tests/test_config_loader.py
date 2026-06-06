"""Tests for herd_common.config_loader.HerdJsonConfigSource."""

import importlib
import json

from pydantic_settings import BaseSettings


def _reload_loader(monkeypatch, config_path: str):
    monkeypatch.setenv("HERD_CONFIG_FILE", config_path)
    from herd_common import config_loader

    return importlib.reload(config_loader)


def test_is_configured_false_without_file(tmp_path, monkeypatch):
    loader = _reload_loader(monkeypatch, str(tmp_path / "missing.json"))
    assert loader.is_configured() is False


def test_is_configured_true_when_file_present(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"POSTGRES_USER": "u"}))
    loader = _reload_loader(monkeypatch, str(config_file))
    assert loader.is_configured() is True


def test_source_returns_nothing_when_file_missing(tmp_path, monkeypatch):
    loader = _reload_loader(monkeypatch, str(tmp_path / "missing.json"))

    class S(BaseSettings):
        secret_key: str = "default"

    source = loader.HerdJsonConfigSource(S)
    assert source() == {}


def test_source_maps_auth_keys(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "AUTH_SECRET_KEY": "secret123",
                "AUTH_ALGORITHM": "HS512",
                "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES": 15,
                "AUTH_REFRESH_TOKEN_EXPIRE_DAYS": 3,
            }
        )
    )
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        secret_key: str = "default"
        algorithm: str = "HS256"
        access_token_expire_minutes: int = 30
        refresh_token_expire_days: int = 7

    source = loader.HerdJsonConfigSource(S)
    values = source()
    assert values["secret_key"] == "secret123"
    assert values["algorithm"] == "HS512"
    assert values["access_token_expire_minutes"] == 15
    assert values["refresh_token_expire_days"] == 3


def test_source_builds_database_url_when_postgres_fields_present(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "POSTGRES_USER": "herd",
                "POSTGRES_PASSWORD": "pw",
                "POSTGRES_DB": "herd",
            }
        )
    )
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        database_url: str = ""

    values = loader.HerdJsonConfigSource(S)()
    assert values["database_url"] == "postgresql+asyncpg://herd:pw@postgres:5432/herd"


def test_source_skips_database_url_when_postgres_incomplete(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"POSTGRES_USER": "herd"}))
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        database_url: str = "fallback"

    values = loader.HerdJsonConfigSource(S)()
    assert "database_url" not in values


def test_source_ignores_fields_not_on_model(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"UNRELATED_KEY": "value", "POSTGRES_USER": "u"}))
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        postgres_user: str = ""

    values = loader.HerdJsonConfigSource(S)()
    assert values == {"postgres_user": "u"}


def test_get_field_value_returns_tuple(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://example"}))
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        cors_origins: str = ""

    source = loader.HerdJsonConfigSource(S)
    field = S.model_fields["cors_origins"]
    val, name, is_set = source.get_field_value(field, "cors_origins")
    assert (val, name, is_set) == ("https://example", "cors_origins", True)

    missing_val, missing_name, missing_is_set = source.get_field_value(field, "other")
    assert missing_val is None
    assert missing_name == "other"
    assert missing_is_set is False
