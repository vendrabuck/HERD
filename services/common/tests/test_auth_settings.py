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
    SECRET_KEY_UNUSABLE_MESSAGE,
    AuthSecretKey,
    JwtAlgorithm,
    validate_auth_secret,
    validate_jwt_algorithm,
)
from pydantic import BaseModel, ValidationError

LOGGER_NAME = "herd_common.auth_settings"

EXPECTED_SECRET_MESSAGE = (
    "AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env, or in "
    "the config UI if a configuration was saved there, to a random value, for "
    "example the output of openssl rand -hex 32"
)
EXPECTED_UNUSABLE_MESSAGE = (
    "AUTH_SECRET_KEY cannot be used as an HMAC secret; set it in .env, or in the "
    "config UI if a configuration was saved there, to a random value, for example "
    "the output of openssl rand -hex 32"
)
EXPECTED_ALGORITHM_MESSAGE = "AUTH_ALGORITHM must be one of HS256, HS384, HS512"


class _Model(BaseModel):
    secret_key: AuthSecretKey
    algorithm: JwtAlgorithm = "HS256"


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def test_messages_are_the_documented_wording():
    assert SECRET_KEY_REFUSED_MESSAGE == EXPECTED_SECRET_MESSAGE
    assert SECRET_KEY_UNUSABLE_MESSAGE == EXPECTED_UNUSABLE_MESSAGE
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
        "\u200b",
        "\ufeff",
        " \u200b\ufeff\t",
        "\u200b" * 40,
    ],
    ids=[
        "empty",
        "space",
        "whitespace",
        "shipped-placeholder",
        "padded-placeholder",
        "other-placeholder-wording",
        "bare-angle-brackets",
        "zero-width-space",
        "byte-order-mark",
        "invisible-and-space",
        "forty-zero-width-spaces",
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


# Shapes jose will not sign and verify with as an HMAC secret: a PEM public key,
# an RSA public key, a certificate, and an ssh public key are refused while the
# key is built; a JSON-shaped value signs but is read as a JSON Web Key on
# verify, so no token it signs verifies.
_UNUSABLE_SECRETS = {
    "pem-public-key": "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQc=\n"
    "-----END PUBLIC KEY-----",
    "rsa-public-key": "-----BEGIN RSA PUBLIC KEY-----\nMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo\n"
    "-----END RSA PUBLIC KEY-----",
    "certificate": "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUZ2Vu\n"
    "-----END CERTIFICATE-----",
    "ssh-public-key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7 user@host",
    "json-web-key": '{"kty": "oct", "k": "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0"}',
    "json-object": '{"secret": "0123456789abcdef0123456789abcdef"}',
}


@pytest.mark.parametrize("name", sorted(_UNUSABLE_SECRETS))
def test_secret_jose_cannot_sign_and_verify_with_is_refused(name, caplog):
    caplog.set_level(logging.DEBUG)
    value = _UNUSABLE_SECRETS[name]
    with pytest.raises(ValidationError) as exc_info:
        _Model(secret_key=value)
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("secret_key",)
    assert errors[0]["msg"] == f"Value error, {EXPECTED_UNUSABLE_MESSAGE}"
    # The round trip neither logs nor echoes the value, or any line of it.
    assert caplog.records == []
    assert all(line not in errors[0]["msg"] for line in value.splitlines())


def test_the_round_trip_signs_and_verifies_with_the_value_itself(monkeypatch):
    seen: list = []
    real_encode = auth_settings.jwt.encode
    real_decode = auth_settings.jwt.decode

    def recording_encode(claims, key, algorithm):
        seen.append(("encode", key, algorithm))
        return real_encode(claims, key, algorithm=algorithm)

    def recording_decode(token, key, algorithms):
        seen.append(("decode", key, algorithms))
        return real_decode(token, key, algorithms=algorithms)

    monkeypatch.setattr(auth_settings.jwt, "encode", recording_encode)
    monkeypatch.setattr(auth_settings.jwt, "decode", recording_decode)
    value = "k" * 40
    assert validate_auth_secret(value) == value
    assert seen == [("encode", value, "HS256"), ("decode", value, ["HS256"])]


def test_a_blank_or_placeholder_secret_is_refused_before_the_round_trip(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the round trip ran")

    monkeypatch.setattr(auth_settings.jwt, "encode", fail)
    for value in ("", "<secret key goes here>", "\u200b"):
        with pytest.raises(ValueError) as exc_info:
            validate_auth_secret(value)
        assert str(exc_info.value) == EXPECTED_SECRET_MESSAGE


def test_a_short_secret_that_jose_cannot_use_is_refused_without_the_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    with pytest.raises(ValueError) as exc_info:
        validate_auth_secret("ssh-rsa AAAA")
    assert str(exc_info.value) == EXPECTED_UNUSABLE_MESSAGE
    assert _records(caplog) == []


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


def test_settings_refuse_the_placeholder_from_a_saved_config_file(monkeypatch, tmp_path):
    # A file saved in the config UI (no bootstrap marker) outranks the
    # environment, so a placeholder there is refused even when env is good.
    config_file = tmp_path / "config.json"
    config_file.write_text('{"AUTH_SECRET_KEY": "<secret key goes here>"}')
    from herd_common import base_settings, config_loader

    # Patched, not reloaded, so the path is restored after the test.
    monkeypatch.setattr(config_loader, "CONFIG_FILE", str(config_file))

    class Settings(base_settings.HerdBaseSettings):
        model_config = {"env_file": None}
        secret_key: auth_settings.AuthSecretKey

    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert [(e["loc"], e["msg"]) for e in exc_info.value.errors()] == [
        (("secret_key",), f"Value error, {EXPECTED_SECRET_MESSAGE}")
    ]


def test_settings_refuse_the_placeholder_from_env(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "<secret key goes here>")
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    assert exc_info.value.errors()[0]["msg"] == f"Value error, {EXPECTED_SECRET_MESSAGE}"


def test_settings_refuse_an_unusable_secret_from_env(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", _UNUSABLE_SECRETS["ssh-public-key"])
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    assert [(e["loc"], e["msg"]) for e in exc_info.value.errors()] == [
        (("secret_key",), f"Value error, {EXPECTED_UNUSABLE_MESSAGE}")
    ]


def test_settings_error_text_names_the_field_and_rule_but_not_the_value(monkeypatch, tmp_path):
    settings_cls = _settings_cls(monkeypatch, tmp_path)
    value = "<my-real-Secr3t-value-0123456789abcdef>"
    monkeypatch.setenv("SECRET_KEY", value)
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    text = str(exc_info.value)
    assert "secret_key" in text
    assert EXPECTED_SECRET_MESSAGE in text
    assert "Secr3t" not in text
    assert "input_value" not in text


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
