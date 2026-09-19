"""Unit tests for herd_common.version (issue #846).

Covers the three helpers plus the registered route: a real distribution's
version resolves to its pyproject's PEP 440 string, an unknown distribution
falls back to "0+unknown" instead of raising, HERD_BUILD/HERD_BUILD_DATE are
read at call time (set, unset, and empty-string each), and the route through
a FastAPI TestClient returns exactly the four contract keys.
"""

import importlib.metadata

import pytest
from fastapi import FastAPI
from herd_common.version import (
    add_version_route,
    build_info,
    service_version,
    version_payload,
)
from httpx import ASGITransport, AsyncClient


def test_service_version_resolves_real_distribution():
    # herd-common is always installed in this test environment; its version
    # must match the one in services/common/pyproject.toml.
    assert service_version("herd-common") == importlib.metadata.version("herd-common")


def test_service_version_unknown_distribution_falls_back():
    assert service_version("herd-does-not-exist-nope") == "0+unknown"


def test_build_info_unset(monkeypatch):
    monkeypatch.delenv("HERD_BUILD", raising=False)
    monkeypatch.delenv("HERD_BUILD_DATE", raising=False)
    assert build_info() == {"build": "dev", "build_date": None}


def test_build_info_set(monkeypatch):
    monkeypatch.setenv("HERD_BUILD", "v0.5.0-16-gb29c8812")
    monkeypatch.setenv("HERD_BUILD_DATE", "2026-09-15T00:00:00Z")
    assert build_info() == {
        "build": "v0.5.0-16-gb29c8812",
        "build_date": "2026-09-15T00:00:00Z",
    }


def test_build_info_empty_string_counts_as_unset(monkeypatch):
    monkeypatch.setenv("HERD_BUILD", "")
    monkeypatch.setenv("HERD_BUILD_DATE", "")
    assert build_info() == {"build": "dev", "build_date": None}


def test_version_payload_shape(monkeypatch):
    monkeypatch.delenv("HERD_BUILD", raising=False)
    monkeypatch.delenv("HERD_BUILD_DATE", raising=False)
    payload = version_payload("acl", "herd-does-not-exist-nope")
    assert payload == {
        "service": "acl",
        "version": "0+unknown",
        "build": "dev",
        "build_date": None,
    }


def _build_app(**kwargs) -> FastAPI:
    app = FastAPI()
    add_version_route(app, service="acl", distribution="herd-common", **kwargs)
    return app


@pytest.mark.asyncio
async def test_route_returns_exactly_four_keys(monkeypatch):
    monkeypatch.setenv("HERD_BUILD", "v0.5.0-16-gb29c8812")
    monkeypatch.setenv("HERD_BUILD_DATE", "2026-09-15T00:00:00Z")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"service", "version", "build", "build_date"}
    assert data["service"] == "acl"
    assert data["version"] == importlib.metadata.version("herd-common")
    assert data["build"] == "v0.5.0-16-gb29c8812"
    assert data["build_date"] == "2026-09-15T00:00:00Z"


@pytest.mark.asyncio
async def test_route_included_in_schema_by_default():
    app = _build_app()
    assert "/version" in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_route_can_be_excluded_from_schema():
    app = _build_app(include_in_schema=False)
    assert "/version" not in app.openapi()["paths"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/version")
    assert resp.status_code == 200
