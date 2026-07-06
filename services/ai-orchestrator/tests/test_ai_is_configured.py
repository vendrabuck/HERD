"""Tests for the ai_is_configured() helper and the get_ai_client() factory.

ai_is_configured() drives the three route 503 gates and the /status endpoint;
get_ai_client() picks the provider class. The anthropic provider is configured
by either a hosted API key OR a base_url for a local keyless endpoint.
"""

import pytest
from app import config as config_module
from app.services.ai_client import (
    AI_NOT_CONFIGURED_DETAIL,
    ai_is_configured,
    get_ai_client,
)
from app.services.providers.anthropic_provider import AnthropicProvider
from app.services.providers.openai_provider import OpenAICompatProvider
from fastapi import HTTPException

# --- ai_is_configured: anthropic provider ---


def test_anthropic_configured_with_key_only(monkeypatch):
    """Hosted Anthropic API: a key with no base_url is enough."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-x")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    assert ai_is_configured() is True


def test_anthropic_configured_with_base_url_only(monkeypatch):
    """Local keyless Anthropic-compatible endpoint (vLLM): base_url alone is enough."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "https://vllm:8000/v1")
    assert ai_is_configured() is True


def test_anthropic_unconfigured_when_key_and_base_url_blank(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    assert ai_is_configured() is False


# --- ai_is_configured: openai_compat provider ---


def test_openai_compat_configured_with_base_url_only(monkeypatch):
    """Local servers (vLLM, Ollama) often don't need an API key."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "http://vllm:8000/v1")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
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
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_model", "claude-test")
    client = get_ai_client()
    assert isinstance(client._provider, AnthropicProvider)


def test_get_ai_client_anthropic_keyless_local_endpoint(monkeypatch):
    """A keyless local Anthropic-compatible endpoint builds a provider from
    base_url alone; the blank key is replaced by the "EMPTY" placeholder."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "https://vllm:8000/v1")
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


def test_get_ai_client_503_on_unknown_provider(monkeypatch):
    """An unrecognized ai_provider is the same state ai_is_configured() reports as
    unconfigured, so get_ai_client degrades to a 503 (issue #245) with the exact
    unconfigured-gate detail rather than escaping the dependency as a 500."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "totally-not-real")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-whatever")
    with pytest.raises(HTTPException) as exc:
        get_ai_client()
    assert exc.value.status_code == 503
    assert exc.value.detail == AI_NOT_CONFIGURED_DETAIL
    assert ai_is_configured() is False


def test_get_ai_client_503_on_construction_failure(monkeypatch):
    """A provider that raises while being CONSTRUCTED (issue #280: an ai_ca_cert
    path that does not exist blows up in the TLS context build) degrades to a 503
    at the dependency boundary, not a 500 before any gate is reached."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-x")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_ca_cert", "/nonexistent/path/to/ca-bundle.pem")
    with pytest.raises(HTTPException) as exc:
        get_ai_client()
    assert exc.value.status_code == 503
    assert exc.value.detail == AI_NOT_CONFIGURED_DETAIL
