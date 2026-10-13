"""The config service's copy of the access-token setting rules.

app/auth_rules.py is a standard-library copy of two of
herd_common.auth_settings' rules (the config image has no herd_common). The
first tests pin the copy's messages and algorithm list to the shared module on
the host, where both are importable, so the two cannot drift. The rest pin what
the config UI save and the first-boot bootstrap do with a refused value: report
the shared message and write nothing.
"""

import hashlib
import json
import os

import pytest
from app import auth_rules, config_store
from app.config_schema import REQUIRED_KEYS
from app.config_store import load_config, save_config
from app.main import app
from httpx import ASGITransport, AsyncClient

CFG_PASSWORD = "test-config-pass"

EXPECTED_SECRET_MESSAGE = (
    "AUTH_SECRET_KEY is unset, blank, or still a placeholder; set it in .env, or in "
    "the config UI if a configuration was saved there, to a random value, for "
    "example the output of openssl rand -hex 32"
)
EXPECTED_ALGORITHM_MESSAGE = "AUTH_ALGORITHM must be one of HS256, HS384, HS512"

GOOD_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef"


def _values(**overrides):
    values = {
        "POSTGRES_USER": "herd",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "herddb",
        "AUTH_SECRET_KEY": GOOD_SECRET,
        "INTERNAL_API_TOKEN": "tok123",
    }
    values.update(overrides)
    return values


def _file_digest() -> str | None:
    if not os.path.exists(config_store.CONFIG_FILE):
        return None
    with open(config_store.CONFIG_FILE, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --- the copy matches the shared module ---


def test_messages_are_the_shared_modules_spelled_identically():
    from herd_common import auth_settings as shared

    assert auth_rules.SECRET_KEY_REFUSED_MESSAGE == shared.SECRET_KEY_REFUSED_MESSAGE
    assert auth_rules.ALGORITHM_REFUSED_MESSAGE == shared.ALGORITHM_REFUSED_MESSAGE
    assert auth_rules.ALLOWED_JWT_ALGORITHMS == shared.ALLOWED_JWT_ALGORITHMS
    assert auth_rules.SECRET_KEY_REFUSED_MESSAGE == EXPECTED_SECRET_MESSAGE
    assert auth_rules.ALGORITHM_REFUSED_MESSAGE == EXPECTED_ALGORITHM_MESSAGE


_REFUSED_SECRETS = [
    "",
    "   ",
    "​",
    "﻿",
    "<secret key goes here>",
    "  <secret key goes here>  ",
    "<>",
    None,
    12345,
]


@pytest.mark.parametrize("value", _REFUSED_SECRETS)
def test_the_copy_refuses_the_secrets_the_shared_rule_refuses(value):
    assert auth_rules.secret_key_refused(value) is True
    if isinstance(value, str):
        from herd_common.auth_settings import SECRET_KEY_REFUSED_MESSAGE, validate_auth_secret

        with pytest.raises(ValueError) as exc_info:
            validate_auth_secret(value)
        assert str(exc_info.value) == SECRET_KEY_REFUSED_MESSAGE


@pytest.mark.parametrize("value", [GOOD_SECRET, "a<b>" + "x" * 40, "<" + "x" * 40])
def test_the_copy_admits_a_secret_the_shared_rule_admits(value):
    from herd_common.auth_settings import validate_auth_secret

    assert auth_rules.secret_key_refused(value) is False
    assert validate_auth_secret(value) == value


@pytest.mark.parametrize("value", ["RS256", "none", "hs256", " HS256", 256])
def test_the_copy_refuses_other_algorithms(value):
    assert auth_rules.algorithm_refused(value) is True


@pytest.mark.parametrize("value", ["HS256", "HS384", "HS512", "", None])
def test_the_copy_admits_the_three_algorithms_and_unset(value):
    assert auth_rules.algorithm_refused(value) is False


# --- save_config ---


@pytest.mark.parametrize(
    "secret",
    ["", "   ", "<secret key goes here>", "​"],
    ids=["empty", "blank", "placeholder", "zero-width-space"],
)
def test_save_refuses_a_blank_or_placeholder_secret_and_writes_nothing(secret):
    assert save_config(_values(AUTH_SECRET_KEY=secret)) == [EXPECTED_SECRET_MESSAGE]
    assert _file_digest() is None


def test_save_refuses_a_missing_secret_with_the_shared_message():
    values = _values()
    del values["AUTH_SECRET_KEY"]
    assert save_config(values) == [EXPECTED_SECRET_MESSAGE]
    assert _file_digest() is None


def test_save_refuses_an_rs256_algorithm_and_writes_nothing():
    assert save_config(_values(AUTH_ALGORITHM="RS256")) == [EXPECTED_ALGORITHM_MESSAGE]
    assert _file_digest() is None


def test_save_reports_every_refusal_together():
    values = _values(POSTGRES_USER="", AUTH_SECRET_KEY="<x>", AUTH_ALGORITHM="RS256")
    assert save_config(values) == [
        "POSTGRES_USER is required",
        EXPECTED_SECRET_MESSAGE,
        EXPECTED_ALGORITHM_MESSAGE,
    ]
    assert _file_digest() is None


def test_save_refusal_leaves_an_existing_file_unchanged():
    assert save_config(_values()) == []
    before = _file_digest()
    assert save_config(_values(AUTH_SECRET_KEY="<secret key goes here>")) != []
    assert _file_digest() == before
    assert load_config()["AUTH_SECRET_KEY"] == GOOD_SECRET


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512", ""])
def test_save_with_a_good_secret_and_algorithm_writes(algorithm):
    assert save_config(_values(AUTH_ALGORITHM=algorithm)) == []
    assert load_config()["AUTH_SECRET_KEY"] == GOOD_SECRET
    assert load_config()["AUTH_ALGORITHM"] == algorithm


# --- PUT /settings ---


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login(client) -> dict:
    resp = await client.post("/login", json={"password": CFG_PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"AUTH_SECRET_KEY": ""}, EXPECTED_SECRET_MESSAGE),
        ({"AUTH_SECRET_KEY": "<secret key goes here>"}, EXPECTED_SECRET_MESSAGE),
        ({"AUTH_ALGORITHM": "RS256"}, EXPECTED_ALGORITHM_MESSAGE),
    ],
    ids=["blank", "placeholder", "rs256"],
)
async def test_put_refuses_with_the_shared_message_and_leaves_the_file(
    async_client, overrides, message
):
    async with async_client as client:
        headers = await _login(client)
        good = await client.put("/settings", json={"values": _values()}, headers=headers)
        assert good.status_code == 200
        before = _file_digest()
        resp = await client.put("/settings", json={"values": _values(**overrides)}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"errors": [message]}
    assert _file_digest() == before


@pytest.mark.asyncio
async def test_put_keeping_a_stored_placeholder_secret_is_refused(async_client):
    # A file written before this rule can hold the placeholder; a save that
    # keeps the masked secret resolves to it and is refused, so the operator
    # has to enter a real one.
    os.makedirs(os.path.dirname(config_store.CONFIG_FILE), exist_ok=True)
    with open(config_store.CONFIG_FILE, "w") as f:
        json.dump(_values(AUTH_SECRET_KEY="<secret key goes here>"), f)
    before = _file_digest()
    async with async_client as client:
        headers = await _login(client)
        resp = await client.put(
            "/settings",
            json={"values": _values(AUTH_SECRET_KEY="********")},
            headers=headers,
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"errors": [EXPECTED_SECRET_MESSAGE]}
    assert _file_digest() == before


@pytest.mark.asyncio
async def test_put_keeping_an_env_placeholder_secret_is_refused(async_client, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET_KEY", "<secret key goes here>")
    async with async_client as client:
        headers = await _login(client)
        resp = await client.put(
            "/settings",
            json={"values": _values(AUTH_SECRET_KEY="********")},
            headers=headers,
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"errors": [EXPECTED_SECRET_MESSAGE]}
    assert _file_digest() is None


@pytest.mark.asyncio
async def test_put_replacing_a_stored_placeholder_with_a_real_secret_saves(async_client):
    os.makedirs(os.path.dirname(config_store.CONFIG_FILE), exist_ok=True)
    with open(config_store.CONFIG_FILE, "w") as f:
        json.dump(_values(AUTH_SECRET_KEY="<secret key goes here>"), f)
    async with async_client as client:
        headers = await _login(client)
        resp = await client.put("/settings", json={"values": _values()}, headers=headers)
    assert resp.status_code == 200
    assert load_config()["AUTH_SECRET_KEY"] == GOOD_SECRET


# --- bootstrap_from_env ---


def _set_env(monkeypatch, **overrides):
    for key, value in _values(**overrides).items():
        monkeypatch.setenv(key, value)
    for key in REQUIRED_KEYS:
        assert os.environ.get(key) is not None


def test_bootstrap_with_a_placeholder_secret_writes_nothing(monkeypatch):
    _set_env(monkeypatch, AUTH_SECRET_KEY="<secret key goes here>")
    assert config_store.bootstrap_from_env() is False
    assert not os.path.exists(config_store.CONFIG_FILE)
    assert not os.path.exists(config_store._bootstrap_marker_file())


def test_bootstrap_with_an_rs256_algorithm_writes_nothing(monkeypatch):
    _set_env(monkeypatch, AUTH_ALGORITHM="RS256")
    assert config_store.bootstrap_from_env() is False
    assert not os.path.exists(config_store.CONFIG_FILE)


def test_bootstrap_with_good_auth_settings_writes(monkeypatch):
    _set_env(monkeypatch, AUTH_ALGORITHM="HS512")
    assert config_store.bootstrap_from_env() is True
    assert load_config()["AUTH_SECRET_KEY"] == GOOD_SECRET
    assert load_config()["AUTH_ALGORITHM"] == "HS512"
