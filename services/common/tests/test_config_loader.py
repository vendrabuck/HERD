"""Tests for herd_common.config_loader.HerdJsonConfigSource."""

import importlib
import json
import logging
import os

import pytest
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


# --- No content of config.json stops a service from starting (#1451) ----------------

# The two WARNING lines, exactly; {path} is filled with the file's path.
_CORRUPT = "Ignoring corrupt HERD config file {{path}} ({cls})"
_NOT_OBJECT = "Ignoring HERD config file {{path}}: not a JSON object ({cls})"

# Every content that is not a readable JSON object, with the WARNING it logs.
# Built as bytes so the not-UTF-8 and the BOM cases are exact; the deep nesting is
# past CPython's default recursion limit and the integer past its 4300-digit limit.
_UNUSABLE_CONTENTS = [
    pytest.param(b"{broken", _CORRUPT.format(cls="JSONDecodeError"), id="unparseable"),
    pytest.param(b"", _CORRUPT.format(cls="JSONDecodeError"), id="empty-file"),
    pytest.param(b"  \n", _CORRUPT.format(cls="JSONDecodeError"), id="whitespace-only"),
    pytest.param(
        b'{"CORS_ORIGINS": "\xff"}', _CORRUPT.format(cls="UnicodeDecodeError"), id="not-utf8"
    ),
    pytest.param(
        b'\xef\xbb\xbf{"CORS_ORIGINS": "x"}', _CORRUPT.format(cls="JSONDecodeError"), id="utf8-bom"
    ),
    pytest.param(
        b"[" * 100_000 + b"]" * 100_000, _CORRUPT.format(cls="RecursionError"), id="nested-too-deep"
    ),
    pytest.param(
        b'{"CORS_ORIGINS": ' + b"9" * 5000 + b"}",
        _CORRUPT.format(cls="ValueError"),
        id="int-over-digit-limit",
    ),
    pytest.param(b"[1, 2]", _NOT_OBJECT.format(cls="list"), id="list"),
    pytest.param(b'"CORS_ORIGINS"', _NOT_OBJECT.format(cls="str"), id="string"),
    pytest.param(b"42", _NOT_OBJECT.format(cls="int"), id="number"),
    pytest.param(b"1.5", _NOT_OBJECT.format(cls="float"), id="float"),
    pytest.param(b"null", _NOT_OBJECT.format(cls="NoneType"), id="null"),
    pytest.param(b"true", _NOT_OBJECT.format(cls="bool"), id="true"),
]


def _loader_warnings(caplog):
    return [r for r in caplog.records if r.name == "herd_common.config_loader"]


@pytest.mark.parametrize(("content", "expected"), _UNUSABLE_CONTENTS)
def test_unusable_config_file_is_ignored_with_one_warning(
    tmp_path, monkeypatch, caplog, content, expected
):
    config_file = tmp_path / "config.json"
    config_file.write_bytes(content)
    loader = _reload_loader(monkeypatch, str(config_file))
    caplog.set_level(logging.WARNING, logger="herd_common.config_loader")

    class S(BaseSettings):
        cors_origins: str = "default"
        database_url: str = ""

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert loader.HerdJsonConfigSource(S)() == {}
    records = _loader_warnings(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    # Names the path and the class, never the content.
    assert records[0].getMessage() == expected.format(path=config_file)


@pytest.mark.parametrize(("content", "expected"), _UNUSABLE_CONTENTS)
def test_unusable_config_file_leaves_every_key_to_the_environment(
    tmp_path, monkeypatch, content, expected
):
    # OPS-SET-3 and OPS-SET-4: the file is treated as missing, in file-first order.
    config_file = tmp_path / "config.json"
    config_file.write_bytes(content)
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-env"
    assert settings.log_level == "INFO"


def test_directory_in_place_of_config_file_is_ignored(tmp_path, monkeypatch, caplog):
    config_dir = tmp_path / "config.json"
    config_dir.mkdir()
    loader = _reload_loader(monkeypatch, str(config_dir))
    caplog.set_level(logging.WARNING, logger="herd_common.config_loader")
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-env"
    records = _loader_warnings(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == _CORRUPT.format(cls="IsADirectoryError").format(
        path=config_dir
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_unreadable_config_file_is_ignored(tmp_path, monkeypatch, caplog):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    config_file.chmod(0)
    loader = _reload_loader(monkeypatch, str(config_file))
    caplog.set_level(logging.WARNING, logger="herd_common.config_loader")
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    try:
        settings = _herd_ordered_settings(loader)()
    finally:
        config_file.chmod(0o600)
    assert settings.cors_origins == "https://from-env"
    records = _loader_warnings(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == _CORRUPT.format(cls="PermissionError").format(
        path=config_file
    )


def test_config_file_is_read_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    # A non-ASCII value reads the same under a C or ASCII locale as under UTF-8:
    # the loader names the encoding instead of inheriting the container's.
    config_file = tmp_path / "config.json"
    config_file.write_bytes('{"CORS_ORIGINS": "https://caf\u00e9.example"}'.encode())
    loader = _reload_loader(monkeypatch, str(config_file))
    real_open = open

    def ascii_default_open(file, mode="r", *args, encoding=None, **kwargs):
        # Stand in for a locale whose default encoding is ASCII.
        return real_open(file, mode, *args, encoding=encoding or "ascii", **kwargs)

    monkeypatch.setattr("builtins.open", ascii_default_open)
    assert loader._load_json() == {"CORS_ORIGINS": "https://caf\u00e9.example"}


@pytest.mark.parametrize(
    ("parts", "not_text"),
    [
        pytest.param({"POSTGRES_USER": 5}, "POSTGRES_USER", id="number-user"),
        pytest.param({"POSTGRES_DB": 1.5}, "POSTGRES_DB", id="float-db"),
        pytest.param({"POSTGRES_DB": {"a": 1}}, "POSTGRES_DB", id="object-db"),
        pytest.param({"POSTGRES_PASSWORD": ["pw"]}, "POSTGRES_PASSWORD", id="list-password"),
        pytest.param({"POSTGRES_PASSWORD": True}, "POSTGRES_PASSWORD", id="true-password"),
        pytest.param(
            {"POSTGRES_PASSWORD": "\ud800"}, "POSTGRES_PASSWORD", id="lone-surrogate-password"
        ),
    ],
)
def test_postgres_part_that_is_not_text_leaves_database_url_underived(
    tmp_path, monkeypatch, caplog, parts, not_text
):
    # A valid object whose POSTGRES_* part is not text: URL.create raised TypeError,
    # the rendering UnicodeEncodeError, at import; a list or boolean password was
    # stringified into a wrong URL. The URL is now left to the environment, with one
    # WARNING naming the key and never the value.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    content = {"POSTGRES_USER": "herd", "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "herd"}
    content.update(parts)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(content))
    loader = _reload_loader(monkeypatch, str(config_file))
    caplog.set_level(logging.WARNING, logger="herd_common.config_loader")

    class S(BaseSettings):
        database_url: str = ""

    assert "database_url" not in loader.HerdJsonConfigSource(S)()
    records = _loader_warnings(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"Not deriving DATABASE_URL from HERD config file {config_file}: {not_text} must be text"
    )


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

    # The settings class must declare database_url: the source emits only
    # declared fields, so over a field-less class the result is empty whether
    # or not the env guard exists (issue #1141).
    class S(BaseSettings):
        database_url: str = ""

    # Control: with DATABASE_URL unset, the same class and file DO get the
    # derived URL, so its absence below is the guard's doing.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    derived = loader.HerdJsonConfigSource(S)()
    assert derived["database_url"] == "postgresql+asyncpg://herd:pw@postgres:5432/herd"

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com:6432/herd")
    values = loader.HerdJsonConfigSource(S)()
    assert "database_url" not in values
