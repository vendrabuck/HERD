"""Tests for the inventory summary fetcher."""

import httpx
import pytest
from app.services.inventory_client import InventorySummary, fetch_inventory_summary


def test_summary_prompt_block_sorted():
    s = InventorySummary({"Bravo": 2, "Alpha": 5})
    block = s.to_prompt_block()
    assert block.splitlines() == [
        "- Alpha: 5 available",
        "- Bravo: 2 available",
    ]


def test_summary_prompt_block_empty():
    s = InventorySummary({})
    assert s.to_prompt_block() == "(no templates available)"


def test_summary_template_names():
    s = InventorySummary({"Alpha": 1, "Bravo": 0})
    assert s.template_names == {"Alpha", "Bravo"}


def test_summary_prompt_block_includes_vendor_model_when_known():
    s = InventorySummary(
        template_counts={"EX4300 Series": 3, "Generic": 1},
        template_identity={
            "EX4300 Series": ("Juniper Networks", "EX4300"),
            "Generic": ("unknown", "unknown"),
        },
    )
    block = s.to_prompt_block()
    assert "- EX4300 Series (Juniper Networks EX4300): 3 available" in block
    # Generic falls through to the bare format because vendor/model are unknown
    assert "- Generic: 1 available" in block


@pytest.mark.asyncio
async def test_fetch_inventory_summary_aggregates_counts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "tpl-1",
                            "name": "EX3400",
                            "vendor": "Juniper Networks",
                            "model": "EX3400",
                        },
                        {
                            "id": "tpl-2",
                            "name": "Ubuntu Client",
                            "vendor": "Canonical",
                            "model": "Ubuntu",
                        },
                    ],
                    "total": 2,
                    "skip": 0,
                    "limit": 500,
                },
            )
        if request.url.path.endswith("/devices"):
            tid = request.url.params.get("template_id")
            total = 5 if tid == "tpl-1" else 2
            return httpx.Response(
                200,
                json={"items": [], "total": total, "skip": 0, "limit": 1},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient

    class PatchedAsync(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.services.inventory_client.httpx.AsyncClient", PatchedAsync)

    summary = await fetch_inventory_summary("test-token")
    assert summary.template_counts == {"EX3400": 5, "Ubuntu Client": 2}
    assert summary.template_identity == {
        "EX3400": ("Juniper Networks", "EX3400"),
        "Ubuntu Client": ("Canonical", "Ubuntu"),
    }


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class PatchedAsync(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.services.inventory_client.httpx.AsyncClient", PatchedAsync)


@pytest.mark.asyncio
async def test_fetch_inventory_summary_raises_on_templates_5xx(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates"):
            return httpx.Response(503, json={"detail": "inventory down"})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_inventory_summary("token")


@pytest.mark.asyncio
async def test_fetch_inventory_summary_raises_on_devices_5xx(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/templates"):
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "tpl-1", "name": "PA"}],
                    "total": 1,
                    "skip": 0,
                    "limit": 500,
                },
            )
        if request.url.path.endswith("/devices"):
            return httpx.Response(500, json={"detail": "db blip"})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_inventory_summary("token")


@pytest.mark.asyncio
async def test_fetch_inventory_summary_raises_on_auth_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_inventory_summary("bad-token")


@pytest.mark.asyncio
async def test_fetch_available_devices_short_circuits_on_zero_count():
    from app.services.inventory_client import fetch_available_devices

    assert await fetch_available_devices("token", "tpl-1", 0) == []
    assert await fetch_available_devices("token", "tpl-1", -5) == []


@pytest.mark.asyncio
async def test_fetch_available_devices_raises_on_5xx(monkeypatch):
    from app.services.inventory_client import fetch_available_devices

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_available_devices("token", "tpl-1", 2)


@pytest.mark.asyncio
async def test_fetch_inventory_summary_forwards_bearer_header(monkeypatch):
    seen_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("authorization", ""))
        if request.url.path.endswith("/templates"):
            return httpx.Response(
                200,
                json={"items": [], "total": 0, "skip": 0, "limit": 500},
            )
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    await fetch_inventory_summary("abc.def.ghi")
    assert seen_headers == ["Bearer abc.def.ghi"]
