"""Unit test for GET /version (issue #846).

integration is the one exception: its OpenAPI document is the published
/api/v1 facade CONTRACT (also pinned in docs/api/v1-openapi.json), pinned at
version="1.0.0" and left untouched. /version is registered with
include_in_schema=False so product build info never enters that contract,
even though the route itself still answers 200.
"""

import importlib.metadata

import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient

DISTRIBUTION = "herd-integration"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_version_answers_200(client):
    resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "integration"
    assert data["version"] == importlib.metadata.version(DISTRIBUTION)


@pytest.mark.asyncio
async def test_version_build_unset_is_dev(client, monkeypatch):
    monkeypatch.delenv("HERD_BUILD", raising=False)
    monkeypatch.delenv("HERD_BUILD_DATE", raising=False)
    resp = await client.get("/version")
    data = resp.json()
    assert data["build"] == "dev"
    assert data["build_date"] is None


@pytest.mark.asyncio
async def test_version_build_set(client, monkeypatch):
    monkeypatch.setenv("HERD_BUILD", "v0.5.0-16-gb29c8812")
    monkeypatch.setenv("HERD_BUILD_DATE", "2026-09-15T00:00:00Z")
    resp = await client.get("/version")
    data = resp.json()
    assert data["build"] == "v0.5.0-16-gb29c8812"
    assert data["build_date"] == "2026-09-15T00:00:00Z"


def test_version_absent_from_openapi_paths():
    assert "/version" not in app.openapi()["paths"]


def test_facade_contract_version_untouched():
    """The published /api/v1 version stays pinned at 1.0.0, independent of
    the product version this issue introduces."""
    assert app.openapi()["info"]["version"] == "1.0.0"
