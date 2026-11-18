"""Tests for herd_common.config_loader.HerdJsonConfigSource."""

import importlib
import json
import logging
import os
import threading

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


# --- A config.json that is not a readable JSON object (#1451) ----------------------

# Every content that is not a readable JSON object, with the reason the WARNING names.
# Built as bytes so the not-UTF-8 and the BOM cases are exact; the deep nesting is
# past CPython's default recursion limit and the integer past its 4300-digit limit.
_UNUSABLE_CONTENTS = [
    pytest.param(b"{broken", "JSONDecodeError", id="unparseable"),
    pytest.param(b"", "JSONDecodeError", id="empty-file"),
    pytest.param(b"  \n", "JSONDecodeError", id="whitespace-only"),
    pytest.param(b'{"CORS_ORIGINS": "\xff"}', "UnicodeDecodeError", id="not-utf8"),
    pytest.param(b'\xef\xbb\xbf{"CORS_ORIGINS": "x"}', "JSONDecodeError", id="utf8-bom"),
    pytest.param(b"[" * 100_000 + b"]" * 100_000, "RecursionError", id="nested-too-deep"),
    pytest.param(
        b'{"CORS_ORIGINS": ' + b"9" * 5000 + b"}", "ValueError", id="int-over-digit-limit"
    ),
    pytest.param(b"[1, 2]", "not a JSON object: list", id="list"),
    pytest.param(b'"CORS_ORIGINS"', "not a JSON object: str", id="string"),
    pytest.param(b"42", "not a JSON object: int", id="number"),
    pytest.param(b"1.5", "not a JSON object: float", id="float"),
    pytest.param(b"null", "not a JSON object: NoneType", id="null"),
    pytest.param(b"true", "not a JSON object: bool", id="true"),
]


def _unusable_path_states():
    """(id, builder, reason) for every path state that is not a readable regular file.

    Each builder makes the state at the given path and returns a cleanup callable.
    """

    def directory(path):
        path.mkdir()
        return lambda: None

    def fifo(path):
        os.mkfifo(path)
        return lambda: None

    def dangling_link(path):
        path.symlink_to(path.parent / "no-such-target.json")
        return lambda: None

    def looping_link(path):
        other = path.parent / "loop-b.json"
        path.symlink_to(other)
        other.symlink_to(path)
        return lambda: None

    def unreadable(path):
        path.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
        path.chmod(0)
        return lambda: path.chmod(0o600)

    states = [
        pytest.param(directory, "not a regular file: directory", id="directory"),
        pytest.param(fifo, "not a regular file: FIFO", id="fifo"),
        pytest.param(dangling_link, "FileNotFoundError", id="dangling-link"),
        pytest.param(looping_link, "OSError", id="looping-link"),
    ]
    if os.geteuid() != 0:  # root reads a mode-000 file
        states.append(pytest.param(unreadable, "PermissionError", id="unreadable"))
    return states


_UNUSABLE_PATH_STATES = _unusable_path_states()


def _loader_warnings(caplog):
    return [r for r in caplog.records if r.name == "herd_common.config_loader"]


def _call_with_deadline(fn, seconds=5.0):
    """fn()'s result, or a test failure when it has not returned in `seconds`.

    A read of a FIFO with no writer blocks forever; a daemon thread keeps such a
    regression a failure instead of a hung run.
    """
    result = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"blocked for over {seconds} s"
    if "error" in result:
        raise result["error"]
    return result["value"]


@pytest.mark.parametrize(("content", "reason"), _UNUSABLE_CONTENTS)
def test_unusable_config_file_is_ignored_with_one_warning(
    tmp_path, monkeypatch, caplog, content, reason
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
    # Names the path and the reason, never the content.
    assert records[0].getMessage() == (
        f"Ignoring unusable HERD config file {config_file} ({reason})"
    )


@pytest.mark.parametrize(("content", "reason"), _UNUSABLE_CONTENTS)
def test_unusable_config_file_leaves_every_key_to_the_environment(
    tmp_path, monkeypatch, content, reason
):
    # OPS-SET-3 and OPS-SET-4: the file is treated as missing, in file-first order.
    config_file = tmp_path / "config.json"
    config_file.write_bytes(content)
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    settings = _herd_ordered_settings(loader)()
    assert settings.cors_origins == "https://from-env"
    assert settings.log_level == "INFO"


@pytest.mark.parametrize(("make_state", "reason"), _UNUSABLE_PATH_STATES)
def test_unusable_path_state_is_ignored_with_one_warning_without_blocking(
    tmp_path, monkeypatch, caplog, make_state, reason
):
    # A path that is not a readable regular file is refused before any read, so a
    # FIFO never blocks import; a dangling or looping link is unusable, not missing.
    config_file = tmp_path / "config.json"
    cleanup = make_state(config_file)
    try:
        loader = _reload_loader(monkeypatch, str(config_file))
        caplog.set_level(logging.WARNING, logger="herd_common.config_loader")
        monkeypatch.setenv("CORS_ORIGINS", "https://from-env")
        settings = _call_with_deadline(_herd_ordered_settings(loader))
    finally:
        cleanup()
    assert settings.cors_origins == "https://from-env"
    records = _loader_warnings(caplog)
    assert [r.getMessage() for r in records] == [
        f"Ignoring unusable HERD config file {config_file} ({reason})"
    ]


def test_missing_config_file_logs_nothing(tmp_path, monkeypatch, caplog):
    loader = _reload_loader(monkeypatch, str(tmp_path / "config.json"))
    caplog.set_level(logging.DEBUG, logger="herd_common.config_loader")
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    assert _herd_ordered_settings(loader)().cors_origins == "https://from-env"
    assert _loader_warnings(caplog) == []


def test_link_to_a_readable_file_is_read(tmp_path, monkeypatch):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"CORS_ORIGINS": "https://from-file"}))
    config_file = tmp_path / "config.json"
    config_file.symlink_to(target)
    loader = _reload_loader(monkeypatch, str(config_file))
    monkeypatch.setenv("CORS_ORIGINS", "https://from-env")

    assert _herd_ordered_settings(loader)().cors_origins == "https://from-file"


def test_config_file_is_read_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    # A non-ASCII value reads the same under a C or ASCII locale as under UTF-8:
    # the loader names the encoding instead of inheriting the container's.
    config_file = tmp_path / "config.json"
    config_file.write_bytes('{"CORS_ORIGINS": "https://café.example"}'.encode())
    loader = _reload_loader(monkeypatch, str(config_file))
    real_open = open

    def ascii_default_open(file, mode="r", *args, encoding=None, **kwargs):
        # Stand in for a locale whose default encoding is ASCII.
        return real_open(file, mode, *args, encoding=encoding or "ascii", **kwargs)

    monkeypatch.setattr("builtins.open", ascii_default_open)
    assert loader._load_json() == {"CORS_ORIGINS": "https://café.example"}


_POSTGRES = {"POSTGRES_USER": "herd", "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "herd"}


def _derived_url(tmp_path, monkeypatch, parts):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({**_POSTGRES, **parts}))
    loader = _reload_loader(monkeypatch, str(config_file))

    class S(BaseSettings):
        database_url: str = ""

    return config_file, loader.HerdJsonConfigSource(S)().get("database_url")


@pytest.mark.parametrize(
    ("parts", "user_password_db"),
    [
        pytest.param({"POSTGRES_PASSWORD": 12345}, ("herd", "12345", "herd"), id="int-password"),
        pytest.param({"POSTGRES_PASSWORD": 1.5}, ("herd", "1.5", "herd"), id="float-password"),
        pytest.param({"POSTGRES_PASSWORD": 0}, ("herd", "0", "herd"), id="zero-password"),
        pytest.param({"POSTGRES_USER": 5}, ("5", "pw", "herd"), id="int-user"),
        pytest.param({"POSTGRES_USER": 2.5}, ("2.5", "pw", "herd"), id="float-user"),
        pytest.param({"POSTGRES_DB": 7}, ("herd", "pw", "7"), id="int-db"),
        pytest.param({"POSTGRES_DB": 1.5}, ("herd", "pw", "1.5"), id="float-db"),
        pytest.param(
            {"POSTGRES_PASSWORD": "a\x00b"}, ("herd", "a\x00b", "herd"), id="nul-password"
        ),
    ],
)
def test_postgres_part_that_is_a_number_builds_the_url_from_its_text(
    tmp_path, monkeypatch, parts, user_password_db
):
    # A numeric password rendered as its digits before #1451 and still does; a
    # numeric user or database, which raised TypeError at import, now does too.
    _, url = _derived_url(tmp_path, monkeypatch, parts)
    parsed = make_url(url)
    assert (parsed.username, parsed.password, parsed.database) == user_password_db
    assert (parsed.host, parsed.port) == ("postgres", 5432)


@pytest.mark.parametrize(
    ("parts", "not_usable"),
    [
        pytest.param({"POSTGRES_PASSWORD": True}, "POSTGRES_PASSWORD", id="true-password"),
        pytest.param({"POSTGRES_PASSWORD": False}, "POSTGRES_PASSWORD", id="false-password"),
        pytest.param({"POSTGRES_PASSWORD": ["pw"]}, "POSTGRES_PASSWORD", id="list-password"),
        pytest.param({"POSTGRES_PASSWORD": {"a": 1}}, "POSTGRES_PASSWORD", id="object-password"),
        pytest.param(
            {"POSTGRES_PASSWORD": "\ud800"}, "POSTGRES_PASSWORD", id="lone-surrogate-password"
        ),
        pytest.param({"POSTGRES_USER": True}, "POSTGRES_USER", id="true-user"),
        pytest.param({"POSTGRES_USER": ["u"]}, "POSTGRES_USER", id="list-user"),
        pytest.param({"POSTGRES_USER": "\udc00"}, "POSTGRES_USER", id="lone-surrogate-user"),
        pytest.param({"POSTGRES_DB": {"a": 1}}, "POSTGRES_DB", id="object-db"),
        pytest.param({"POSTGRES_DB": False}, "POSTGRES_DB", id="false-db"),
        pytest.param(
            {"POSTGRES_USER": [1], "POSTGRES_DB": True}, "POSTGRES_USER, POSTGRES_DB", id="two-keys"
        ),
    ],
)
def test_postgres_part_that_cannot_be_url_text_leaves_database_url_underived(
    tmp_path, monkeypatch, caplog, parts, not_usable
):
    # Before #1451 a non-string user or database raised TypeError and a lone
    # surrogate UnicodeEncodeError at import, and a list, object, or boolean
    # password was stringified into a wrong URL. Now the URL is left to the
    # environment, with one WARNING naming the keys and never the values.
    caplog.set_level(logging.WARNING, logger="herd_common.config_loader")
    config_file, url = _derived_url(tmp_path, monkeypatch, parts)
    assert url is None
    assert [r.getMessage() for r in _loader_warnings(caplog)] == [
        f"Not deriving DATABASE_URL from HERD config file {config_file}: "
        f"{not_usable} must be text or a number"
    ]


@pytest.mark.parametrize("key", ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"])
@pytest.mark.parametrize(
    "value", [None, "", "   ", "\t\n"], ids=["null", "empty", "spaces", "tab-newline"]
)
def test_postgres_part_that_is_unset_leaves_database_url_underived_silently(
    tmp_path, monkeypatch, caplog, key, value
):
    # OPS-SET-4: an empty or whitespace-only string means unset; before this, a
    # whitespace-only part was used and built a URL such as `   :pw@postgres`.
    caplog.set_level(logging.WARNING, logger="herd_common.config_loader")
    _, url = _derived_url(tmp_path, monkeypatch, {key: value})
    assert url is None
    assert _loader_warnings(caplog) == []


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
