import json
import os

from app import config_store
from app.config_schema import REQUIRED_KEYS


def _set_required_env(monkeypatch, overrides=None):
    values = {
        "POSTGRES_USER": "herd",
        "POSTGRES_PASSWORD": "pw",
        "POSTGRES_DB": "herd",
        "AUTH_SECRET_KEY": "secret",
        "INTERNAL_API_TOKEN": "token",
    }
    values.update(overrides or {})
    for key in REQUIRED_KEYS:
        monkeypatch.setenv(key, values.get(key, "placeholder"))
    return values


def test_bootstrap_writes_config_when_required_env_set(monkeypatch):
    seed = _set_required_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CORS_ORIGINS", "https://localhost")

    assert config_store.bootstrap_from_env() is True
    assert os.path.exists(config_store.CONFIG_FILE)

    with open(config_store.CONFIG_FILE) as f:
        written = json.load(f)
    for key in REQUIRED_KEYS:
        assert written[key] == seed[key]
    assert written["ANTHROPIC_API_KEY"] == "sk-test"
    assert written["CORS_ORIGINS"] == "https://localhost"


def test_bootstrap_writes_marker(monkeypatch):
    # The marker keeps the env authoritative until a real config-UI save
    # (herd_common.config_loader.herd_settings_sources checks for it).
    _set_required_env(monkeypatch)

    assert config_store.bootstrap_from_env() is True
    assert os.path.exists(config_store._bootstrap_marker_file())


def test_save_config_clears_bootstrap_marker(monkeypatch):
    seed = _set_required_env(monkeypatch)
    assert config_store.bootstrap_from_env() is True
    assert os.path.exists(config_store._bootstrap_marker_file())

    assert config_store.save_config(seed) == []
    assert not os.path.exists(config_store._bootstrap_marker_file())


def test_bootstrap_skips_when_required_missing(monkeypatch):
    for key in REQUIRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    _set_required_env(monkeypatch)
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)

    assert config_store.bootstrap_from_env() is False
    assert not os.path.exists(config_store.CONFIG_FILE)


def test_bootstrap_skips_blank_required(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AUTH_SECRET_KEY", "   ")

    assert config_store.bootstrap_from_env() is False
    assert not os.path.exists(config_store.CONFIG_FILE)


def test_bootstrap_skips_when_file_exists(monkeypatch):
    _set_required_env(monkeypatch)
    os.makedirs(os.path.dirname(config_store.CONFIG_FILE), exist_ok=True)
    with open(config_store.CONFIG_FILE, "w") as f:
        json.dump({"POSTGRES_USER": "existing"}, f)

    assert config_store.bootstrap_from_env() is False
    with open(config_store.CONFIG_FILE) as f:
        assert json.load(f) == {"POSTGRES_USER": "existing"}


async def test_lifespan_invokes_bootstrap(monkeypatch):
    """The FastAPI lifespan should run bootstrap_from_env on startup."""
    _set_required_env(monkeypatch)

    from app.main import app, lifespan

    async with lifespan(app):
        assert os.path.exists(config_store.CONFIG_FILE)
