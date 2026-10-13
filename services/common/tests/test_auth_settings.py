"""Tests for herd_common.auth_settings: the startup rules for the shared
access-token secret and algorithm.

Every message is compared exactly. The short-secret check is pinned through the
records the module's own logger emits, and through what those records do NOT
carry: the value, or any leading part of it.
"""

import importlib
import logging

import pytest
from herd_common import auth_settings
from herd_common.auth_settings import (
    ALGORITHM_REFUSED_MESSAGE,
    MIN_SECRET_KEY_BYTES,
    SECRET_KEY_REFUSED_MESSAGE,
    AuthSecretKey,
    JwtAlgorithm,
    validate_auth_secret,
    validate_jwt_algorithm,
)
from pydantic import BaseModel, ValidationError

LOGGER_NAME = "herd_common.auth_settings"

EXPECTED_SECRET_MESSAGE = (
    "AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env to a "
    "random value, for example the output of openssl rand -hex 32"
)
EXPECTED_ALGORITHM_MESSAGE = "AUTH_ALGORITHM must be one of HS256, HS384, HS512"


class _Model(BaseModel):
    secret_key: AuthSecretKey
    algorithm: JwtAlgorithm = "HS256"


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def test_messages_are_the_documented_wording():
    assert SECRET_KEY_REFUSED_MESSAGE == EXPECTED_SECRET_MESSAGE
    assert ALGORITHM_REFUSED_MESSAGE == EXPECTED_ALGORITHM_MESSAGE
    assert MIN_SECRET_KEY_BYTES == 32


# --- the secret ---


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "\t\n  ",
        "<secret key goes here>",
        "  <secret key goes here>  ",
        "<your secret>",
        "<>",
    ],
    ids=[
        "empty",
        "space",
        "whitespace",
        "shipped-placeholder",
        "padded-placeholder",
        "other-placeholder-wording",
        "bare-angle-brackets",
    ],
)
def test_secret_refused_with_the_fixed_message(value, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    with pytest.raises(ValidationError) as exc_info:
        _Model(secret_key=value)
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("secret_key",)
    assert errors[0]["msg"] == f"Value error, {EXPECTED_SECRET_MESSAGE}"
    assert _records(caplog) == []


@pytest.mark.parametrize(
    "value",
    ["a<b>" + "x" * 40, "<" + "x" * 40, "x" * 40 + ">", "a" * 40 + " <placeholder>"],
    ids=["angle-inside", "leading-angle-only", "trailing-angle-only", "angle-suffix"],
)
def test_secret_with_angle_brackets_not_wrapping_it_loads(value):
    assert _Model(secret_key=value).secret_key == value


def test_secret_of_31_bytes_loads_and_logs_one_warning_with_only_the_length(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    value = "q7" + "Z" * 29
    assert len(value.encode("utf-8")) == 31

    assert _Model(secret_key=value).secret_key == value

    records = _records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "AUTH_SECRET_KEY is 31 bytes; use at least 32 bytes, for example the output of "
        "openssl rand -hex 32"
    )
    assert record.action == "auth_secret_key_short"
    assert record.byte_length == 31
    rendered = record.getMessage() + repr(record.__dict__)
    assert value not in rendered
    assert "q7Z" not in rendered


def test_secret_length_is_counted_in_utf8_bytes(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    # 16 two-byte characters: 16 characters, 32 bytes, so no warning.
    value = "é" * 16
    assert len(value) == 16 and len(value.encode("utf-8")) == 32
    assert _Model(secret_key=value).secret_key == value
    assert _records(caplog) == []


def test_secret_of_32_bytes_loads_and_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    value = "k" * 32
    assert _Model(secret_key=value).secret_key == value
    assert _records(caplog) == []


def test_secret_is_returned_unchanged_including_surrounding_space():
    value = "  " + "k" * 40 + "  "
    assert validate_auth_secret(value) == value


# --- the algorithm ---


@pytest.mark.parametrize("value", ["HS256", "HS384", "HS512"])
def test_each_hmac_algorithm_is_accepted(value):
    assert _Model(secret_key="k" * 32, algorithm=value).algorithm == value
    assert validate_jwt_algorithm(value) == value


def test_algorithm_defaults_to_hs256():
    assert _Model(secret_key="k" * 32).algorithm == "HS256"


@pytest.mark.parametrize(
    "value",
    ["RS256", "ES256", "none", "None", "", "hs256", " HS256", "HS256 ", "HS1024"],
)
def test_other_algorithms_are_refused_naming_the_three_allowed(value):
    with pytest.raises(ValidationError) as exc_info:
        _Model(secret_key="k" * 32, algorithm=value)
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("algorithm",)
    assert errors[0]["msg"] == f"Value error, {EXPECTED_ALGORITHM_MESSAGE}"


# --- a Settings model reading the environment ---


def _settings_cls(monkeypatch, tmp_path):
    """A stand-in for a service's Settings, with no config.json in reach."""
    monkeypatch.setenv("HERD_CONFIG_FILE", str(tmp_path / "absent-config.json"))
    from herd_common import base_settings, config_loader

    importlib.reload(config_loader)
    base_settings = importlib.reload(base_settings)

    class Settings(base_settings.HerdBaseSettings):
        model_config = {"env_file": None}
        secret_key: auth_settings.AuthSecretKey
        algorithm: auth_settings.JwtAlgorithm = "HS256"

    return Settings


def test_settings_refuse_an_empty_secret_key_env(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "")
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    errors = exc_info.value.errors()
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("secret_key",), f"Value error, {EXPECTED_SECRET_MESSAGE}")
    ]


def test_settings_refuse_the_placeholder_from_env(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "<secret key goes here>")
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    assert exc_info.value.errors()[0]["msg"] == f"Value error, {EXPECTED_SECRET_MESSAGE}"


def test_settings_require_the_secret_key(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    errors = exc_info.value.errors()
    assert [(e["loc"], e["type"]) for e in errors] == [(("secret_key",), "missing")]


def test_settings_refuse_an_rs256_algorithm_env(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("ALGORITHM", "RS256")
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    errors = exc_info.value.errors()
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("algorithm",), f"Value error, {EXPECTED_ALGORITHM_MESSAGE}")
    ]


def test_settings_load_a_good_secret_and_algorithm_from_env(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "k" * 64)
    monkeypatch.setenv("ALGORITHM", "HS512")
    loaded = settings_cls()
    assert loaded.secret_key == "k" * 64
    assert loaded.algorithm == "HS512"
