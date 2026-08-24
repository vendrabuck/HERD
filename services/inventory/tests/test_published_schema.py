"""Deliberate unit tests for app.services.published_schema.

Every consumer test (test_device_configs.py, test_drivers.py) patches the
resolver functions at the router boundary, so the module's own HTTP-handling
branches (the 200 parse-and-validate guard, the non-200 fallback, the
transport-error fallback, and the in-process TTL memo) were never directly
exercised (issue #567). httpx.AsyncClient is routed through httpx.MockTransport
by subclassing it and monkeypatching the module's `httpx.AsyncClient` name, the
same idiom used in notifications/tests/test_contact_client.py and
ai-orchestrator/tests/test_inventory_client.py; no production code changes.
"""

import uuid

import httpx
import pytest
from app.models.driver_package import DriverPackage
from app.services import published_schema as mod


def _make_driver(**overrides) -> DriverPackage:
    """An unpersisted DriverPackage; the resolver only reads plain attributes."""
    defaults = dict(
        id=uuid.uuid4(),
        name=f"driver-{uuid.uuid4().hex[:8]}",
        connection_type="Management",
        filename="driver.zip",
        storage_key="drivers/driver.zip",
        size_bytes=1024,
        sha256=uuid.uuid4().hex,
        uploaded_by="tester",
        supports_dry_run=False,
    )
    defaults.update(overrides)
    return DriverPackage(**defaults)


def _patch_transport(monkeypatch, handler):
    """Route app.services.published_schema's httpx.AsyncClient through a MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class PatchedAsync(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.services.published_schema.httpx.AsyncClient", PatchedAsync)


@pytest.fixture(autouse=True)
def _clear_memo():
    """Every test starts and ends with a clean memo so cases cannot leak state."""
    mod._invalidate_memo()
    yield
    mod._invalidate_memo()


@pytest.mark.asyncio
async def test_valid_200_parses_and_returns_schema(monkeypatch):
    driver = _make_driver()
    published = {
        "type": "object",
        "properties": {"hostname": {"type": "string"}},
        "additionalProperties": False,
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == f"/drivers/{driver.id}/config-schema"
        assert request.headers["X-Internal-Token"] == "test-token"
        assert request.url.params["sha256"] == driver.sha256
        assert request.url.params["filename"] == driver.filename
        assert request.url.params["connection_type"] == driver.connection_type
        return httpx.Response(200, json={"has_schema": True, "schema": published})

    _patch_transport(monkeypatch, handler)

    result = await mod.published_schema_for_driver(driver)
    assert result == published
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_200_malformed_body_falls_back_to_none(monkeypatch):
    """has_schema True but schema is not a dict must not be trusted verbatim."""
    driver = _make_driver()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_schema": True, "schema": "not-a-dict"})

    _patch_transport(monkeypatch, handler)

    result = await mod.published_schema_for_driver(driver)
    assert result is None


@pytest.mark.asyncio
async def test_200_has_schema_false_falls_back_to_none(monkeypatch):
    driver = _make_driver()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_schema": False, "schema": None})

    _patch_transport(monkeypatch, handler)

    result = await mod.published_schema_for_driver(driver)
    assert result is None


@pytest.mark.asyncio
async def test_non_200_falls_back_to_none_with_warning(monkeypatch, caplog):
    driver = _make_driver()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "execution unavailable"})

    _patch_transport(monkeypatch, handler)

    with caplog.at_level("WARNING"):
        result = await mod.published_schema_for_driver(driver)
    assert result is None
    assert any("execution config-schema returned 503" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_transport_error_falls_back_to_none(monkeypatch, caplog):
    driver = _make_driver()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _patch_transport(monkeypatch, handler)

    with caplog.at_level("WARNING"):
        result = await mod.published_schema_for_driver(driver)
    assert result is None
    assert any("could not resolve published schema" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_memo_hit_within_ttl_skips_second_http_call(monkeypatch):
    driver = _make_driver()
    published = {"type": "object", "properties": {}}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"has_schema": True, "schema": published})

    _patch_transport(monkeypatch, handler)

    first = await mod.published_schema_for_driver(driver)
    second = await mod.published_schema_for_driver(driver)

    assert first == published
    assert second == published
    assert calls["n"] == 1  # second call served from the memo, no second request


@pytest.mark.asyncio
async def test_invalidate_memo_forces_a_fresh_fetch(monkeypatch):
    """The _invalidate_memo test hook (previously caller-less) clears the memo
    so a case can force a second real fetch within the same TTL window."""
    driver = _make_driver()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"has_schema": True, "schema": {"type": "object"}})

    _patch_transport(monkeypatch, handler)

    await mod.published_schema_for_driver(driver)
    assert calls["n"] == 1

    mod._invalidate_memo()

    await mod.published_schema_for_driver(driver)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_memo_is_keyed_per_driver_sha256(monkeypatch):
    """A different driver (different id/sha256) must not hit another driver's memo entry."""
    driver_a = _make_driver()
    driver_b = _make_driver()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"has_schema": True, "schema": {"type": "object"}})

    _patch_transport(monkeypatch, handler)

    await mod.published_schema_for_driver(driver_a)
    await mod.published_schema_for_driver(driver_b)

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_published_schema_for_device_returns_none_when_no_driver():
    """The driver-is-None early return in published_schema_for_device: a device
    whose template has no linked driver must resolve to None without any HTTP
    attempt (no transport is patched, so a real call would fail the test)."""

    class _NoDriverTemplate:
        driver = None

    class _Device:
        template = _NoDriverTemplate()

    result = await mod.published_schema_for_device(_Device())
    assert result is None


@pytest.mark.asyncio
async def test_published_schema_for_device_returns_none_when_no_template():
    class _Device:
        template = None

    result = await mod.published_schema_for_device(_Device())
    assert result is None


@pytest.mark.asyncio
async def test_published_schema_for_device_delegates_to_driver_fetch(monkeypatch):
    driver = _make_driver()
    published = {"type": "object", "properties": {"ip": {"type": "string"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_schema": True, "schema": published})

    _patch_transport(monkeypatch, handler)

    class _Template:
        driver = None

    template = _Template()
    template.driver = driver

    class _Device:
        pass

    device = _Device()
    device.template = template

    result = await mod.published_schema_for_device(device)
    assert result == published
