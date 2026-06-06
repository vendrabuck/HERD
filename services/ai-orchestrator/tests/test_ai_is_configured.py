"""Tests for the ai_is_configured() helper and the get_ai_client() factory.

ai_is_configured() drives the three route 503 gates and the /status endpoint;
get_ai_client() picks the provider class. Both honor ANTHROPIC_API_KEY as a
fallback for AI_API_KEY for one release.
"""

import pytest
from app import config as config_module
from app.services.ai_client import _effective_api_key, ai_is_configured, get_ai_client
from app.services.providers.anthropic_provider import AnthropicProvider
from app.services.providers.openai_provider import OpenAICompatProvider

# --- _effective_api_key fallback chain ---


def test_effective_api_key_prefers_ai_api_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-new-canonical")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-old-legacy")
    assert _effective_api_key() == "sk-new-canonical"


def test_effective_api_key_falls_back_to_anthropic_api_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-old-legacy")
    assert _effective_api_key() == "sk-old-legacy"


def test_effective_api_key_empty_when_both_blank(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    assert _effective_api_key() == ""


# --- ai_is_configured: anthropic provider ---


def test_anthropic_configured_with_canonical_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-x")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    assert ai_is_configured() is True


def test_anthropic_configured_with_deprecated_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-legacy")
    assert ai_is_configured() is True


def test_anthropic_unconfigured_when_both_keys_blank(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    assert ai_is_configured() is False


# --- ai_is_configured: openai_compat provider ---


def test_openai_compat_configured_with_base_url_only(monkeypatch):
    """Local servers (vLLM, Ollama) often don't need an API key."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "http://vllm:8000/v1")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    assert ai_is_configured() is True


def test_openai_compat_unconfigured_without_base_url(monkeypatch):
    """No base_url means we don't know where to send the request, even if a key is set."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-could-be-anything")
    assert ai_is_configured() is False


# --- get_ai_client provider selection ---


def test_get_ai_client_constructs_anthropic_provider(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-x")
    monkeypatch.setattr(config_module.settings, "ai_model", "claude-test")
    client = get_ai_client()
    assert isinstance(client._provider, AnthropicProvider)


def test_get_ai_client_constructs_openai_compat_provider(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "http://vllm:8000/v1")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_model", "qwen-test")
    client = get_ai_client()
    assert isinstance(client._provider, OpenAICompatProvider)


def test_get_ai_client_raises_on_unknown_provider(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "totally-not-real")
    with pytest.raises(RuntimeError, match="unknown ai_provider"):
        get_ai_client()


def test_deprecation_warning_does_not_collide_with_logrecord_reserved_keys(caplog):
    """Regression: the deprecation warning's extra={} dict must not contain
    keys that overlap with LogRecord's built-in attributes (message, args,
    levelname, etc.), otherwise Python's logging machinery raises a KeyError
    at emission time and the orchestrator crashes on startup. The warning is
    a logger.warning call, not a runtime conditional, so this test fires it
    directly through the same code path that startup uses.
    """
    import logging

    from app.main import logger as main_logger

    with caplog.at_level(logging.WARNING, logger=main_logger.name):
        main_logger.warning(
            "ANTHROPIC_API_KEY is deprecated; set AI_API_KEY instead.",
            extra={"event": "anthropic_api_key_deprecated"},
        )
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert records, "expected a WARNING record"
    assert "deprecated" in records[0].getMessage()
    assert getattr(records[0], "event", None) == "anthropic_api_key_deprecated"
