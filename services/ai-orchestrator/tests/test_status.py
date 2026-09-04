import pytest
from app import config as config_module
from app.main import app
from app.services import ai_client as ai_client_module
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _fresh_construction_cache():
    """Each test gets an isolated, expired provider-construction cache.

    Without this, the module-level cache (issue #606's rate limit) would
    carry a construction result from one test's settings into the next
    within the same 30s TTL window.
    """
    cache = ai_client_module._ProviderConstructionCache()
    ai_client_module._provider_construction_cache = cache
    yield cache


@pytest.mark.asyncio
async def test_status_enabled_when_anthropic_key_set(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["provider"] == "anthropic"
    assert body["model"] == config_module.settings.ai_model
    assert body["degraded"] is False
    assert body["reason"] is None


@pytest.mark.asyncio
async def test_status_disabled_when_anthropic_key_blank(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["provider"] == "anthropic"
    # Settings-unconfigured path: never attempts construction, so degraded
    # stays false/absent-equivalent rather than reporting a construction
    # failure that never happened.
    assert body["degraded"] is False
    assert body["reason"] is None


@pytest.mark.asyncio
async def test_status_enabled_for_anthropic_keyless_local_endpoint(async_client, monkeypatch):
    """A local keyless Anthropic-compatible endpoint (vLLM) is configured by base_url alone."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "https://vllm:8000/v1")
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
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["provider"] == "openai_compat"


@pytest.mark.asyncio
async def test_status_purpose_classification_true_only_when_flag_and_enabled(
    async_client, monkeypatch
):
    """issue #646 phase 2: purpose_classification is additive and true only
    when the flag is on AND the provider is configured (enabled)."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")

    monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", False)
    async with async_client as client:
        resp = await client.get("/status")
        assert resp.json()["purpose_classification"] is False

        monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", True)
        resp = await client.get("/status")
        body = resp.json()
        assert body["enabled"] is True
        assert body["purpose_classification"] is True


@pytest.mark.asyncio
async def test_status_purpose_classification_false_when_unconfigured_even_with_flag_on(
    async_client, monkeypatch
):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", True)
    async with async_client as client:
        resp = await client.get("/status")
    body = resp.json()
    assert body["enabled"] is False
    assert body["purpose_classification"] is False


@pytest.mark.asyncio
async def test_status_disabled_for_unknown_provider(async_client, monkeypatch):
    """An unrecognized ai_provider (typo) reports enabled=false, the same
    unconfigured state get_ai_client degrades to a 503 (issue #245). Proves the
    status endpoint and the dependency gate agree on an unknown provider even
    when a key is set."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "athropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_status_disabled_for_openai_compat_without_base_url(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-could-be-anything")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# --- degraded state: settings look configured but construction fails (issue #606) ---


class _MarkerError(RuntimeError):
    """Raised with a distinctive message the response body must never echo."""


@pytest.mark.asyncio
async def test_status_degraded_when_construction_raises(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")

    def _boom():
        raise _MarkerError("super-secret-base-url-or-key-marker-xyz123")

    monkeypatch.setattr(ai_client_module, "_build_provider", _boom)
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["degraded"] is True
    assert body["reason"] == "_MarkerError"
    # The exception MESSAGE must never appear anywhere in the response body,
    # only the exception class name (it can carry a base URL or key material).
    assert "super-secret-base-url-or-key-marker-xyz123" not in resp.text


@pytest.mark.asyncio
async def test_status_construction_failure_reason_is_real_exception_class(
    async_client, monkeypatch
):
    """Uses the real construction path (a missing ai_ca_cert file, issue #280's
    class of bug) rather than a stub, so the reason string is whatever
    _build_provider genuinely raises, not a test double's shape."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    monkeypatch.setattr(config_module.settings, "ai_ca_cert", "/nonexistent/path/to/ca-bundle.pem")
    async with async_client as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["degraded"] is True
    assert isinstance(body["reason"], str) and body["reason"]
    assert "/nonexistent/path/to/ca-bundle.pem" not in resp.text


@pytest.mark.asyncio
async def test_status_construction_success_after_previous_failure_reports_ok(
    async_client, monkeypatch
):
    monkeypatch.setattr(config_module.settings, "ai_provider", "anthropic")
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-real")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")

    async with async_client as client:
        resp = await client.get("/status")
    body = resp.json()
    assert body["enabled"] is True
    assert body["degraded"] is False
    assert body["reason"] is None


# --- construction cache (issue #606's rate-limit requirement) ---


def test_construction_cache_reuses_result_within_ttl(monkeypatch):
    calls = {"n": 0}

    def _build():
        calls["n"] += 1
        return object()

    monkeypatch.setattr(ai_client_module, "_build_provider", _build)
    clock = {"now": 0.0}
    cache = ai_client_module._ProviderConstructionCache(
        ttl_seconds=30.0, clock=lambda: clock["now"]
    )
    cache.check()
    clock["now"] += 10.0
    cache.check()
    assert calls["n"] == 1


def test_construction_cache_reconstructs_after_ttl_expires(monkeypatch):
    calls = {"n": 0}

    def _build():
        calls["n"] += 1
        return object()

    monkeypatch.setattr(ai_client_module, "_build_provider", _build)
    clock = {"now": 0.0}
    cache = ai_client_module._ProviderConstructionCache(
        ttl_seconds=30.0, clock=lambda: clock["now"]
    )
    cache.check()
    clock["now"] += 30.1
    cache.check()
    assert calls["n"] == 2
