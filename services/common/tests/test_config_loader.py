"""Tests for herd_common.config_loader.HerdJsonConfigSource."""

import importlib
import json

from pydantic_settings import BaseSettings
from sqlalchemy.engine import make_url


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
    monkeypatch.delenv("DATABASE_URL", raising=False)
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


def test_source_url_encodes_special_chars_in_password(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "POSTGRES_USER": "herd",
                "POSTGRES_PASSWORD": "p@ss:w/rd#1",
                "POSTGRES_DB": "herd",
            }
        )
    )
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        database_url: str = ""

    values = loader.HerdJsonConfigSource(S)()
    # The raw f-string would mis-parse @ : / # in the password; URL.create
    # percent-encodes each component so SQLAlchemy round-trips it correctly.
    parsed = make_url(values["database_url"])
    assert parsed.password == "p@ss:w/rd#1"
    assert parsed.host == "postgres"
    assert parsed.port == 5432
    assert parsed.database == "herd"
    assert parsed.username == "herd"
    # Literal special chars must not survive unencoded in the rendered string.
    assert "p@ss:w/rd#1" not in values["database_url"]


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


def test_source_falls_back_when_config_json_is_corrupt(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text("{broken")
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        secret_key: str = "default"

    # A truncated/corrupt mounted config must not raise JSONDecodeError at
    # import/construction time; it falls back to {} so env/defaults apply.
    source = loader.HerdJsonConfigSource(S)
    assert source() == {}


def _herd_ordered_settings(loader):
    """Settings class wired through herd_settings_sources, as every service is."""

    class S(BaseSettings):
        cors_origins: str = "default-origins"
        log_level: str = "INFO"

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return loader.herd_settings_sources(
                settings_cls,
                init_settings,
                env_settings,
                dotenv_settings,
                file_secret_settings,
            )

    return S


def test_herd_sources_file_beats_env(tmp_path, monkeypatch):
    # The config-UI invariant: a key saved to config.json takes effect even
    # though compose injects the same key as a container env var.
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-file"


def test_herd_sources_env_applies_for_keys_absent_from_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = _herd_ordered_settings(loader)()
    assert settings.log_level == "DEBUG"


def test_herd_sources_init_kwargs_beat_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    loader = _reload_loader(monkeypatch, str(config_file))

    settings = _herd_ordered_settings(loader)(cors_origins="https://explicit")
    assert settings.cors_origins == "https://explicit"


def test_herd_sources_pure_env_when_file_missing(tmp_path, monkeypatch):
    # Unit tests and bare processes have no /etc/herd/config.json; the reorder
    # must be a no-op there.
    loader = _reload_loader(monkeypatch, str(tmp_path / "missing.json"))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-env"


def test_herd_sources_env_first_while_bootstrap_marker_present(tmp_path, monkeypatch):
    # An auto-bootstrapped config.json is a copy of the environment, not an
    # operator decision: while the marker exists the env stays authoritative
    # and the file only fills keys the env lacks (its original fallback role).
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"CORS_ORIGINS": "https://from-file", "LOG_LEVEL": "WARNING"})
    )
    (tmp_path / "config.bootstrapped").write_text("marker")
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-env"
    assert settings.log_level == "WARNING"


def test_source_skips_empty_string_values(tmp_path, monkeypatch):
    # An empty string in config.json means "unset": the key must fall through
    # to the environment instead of shadowing it, even in file-first mode.
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "", "LOG_LEVEL": "WARNING"}))
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-env"
    assert settings.log_level == "WARNING"


def test_source_database_url_yields_to_env_database_url(tmp_path, monkeypatch):
    # The derived URL hardcodes the in-compose postgres:5432 host; a deployer
    # pointing at an external DB via DATABASE_URL must keep winning even in
    # file-first mode.
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"POSTGRES_USER": "herd", "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "herd"})
    )
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com:6432/herd")

    values = loader.HerdJsonConfigSource(BaseSettings)()
    assert "database_url" not in values
