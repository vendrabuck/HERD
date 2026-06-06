import pytest
from app import config as config_module
from app.main import app
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_status_enabled_when_anthropic_key_set(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["provider"] == "anthropic"
    assert body["model"] == config_module.settings.ai_model


@pytest.mark.asyncio
async def test_status_disabled_when_anthropic_key_blank(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_status_honors_deprecated_anthropic_api_key_fallback(async_client, monkeypatch):
    """ANTHROPIC_API_KEY is still honored when AI_API_KEY is blank (one-release alias)."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-legacy")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_status_enabled_for_openai_compat_with_base_url(async_client, monkeypatch):
    """openai_compat only needs a base_url; local servers (vLLM, Ollama) may have no key."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "http://vllm:8000/v1")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["provider"] == "openai_compat"


@pytest.mark.asyncio
async def test_status_disabled_for_openai_compat_without_base_url(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-could-be-anything")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
