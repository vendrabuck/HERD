"""Unit test for GET /version (issue #846).

config is the one service that previously passed no version= at all; the
conftest's autouse tmp_config_dir fixture applies here too, though /version
touches no config storage.
"""

import importlib.metadata

import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient

DISTRIBUTION = "herd-config"


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_version(async_client):
    async with async_client as client:
        resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "config"
    assert data["version"] == importlib.metadata.version(DISTRIBUTION)


@pytest.mark.asyncio
async def test_version_build_unset_is_dev(async_client, monkeypatch):
    monkeypatch.delenv("HERD_BUILD", raising=False)
    monkeypatch.delenv("HERD_BUILD_DATE", raising=False)
    async with async_client as client:
        resp = await client.get("/version")
    data = resp.json()
    assert data["build"] == "dev"
    assert data["build_date"] is None


@pytest.mark.asyncio
async def test_version_build_set(async_client, monkeypatch):
    monkeypatch.setenv("HERD_BUILD", "v0.5.0-16-gb29c8812")
    monkeypatch.setenv("HERD_BUILD_DATE", "2026-09-15T00:00:00Z")
    async with async_client as client:
        resp = await client.get("/version")
    data = resp.json()
    assert data["build"] == "v0.5.0-16-gb29c8812"
    assert data["build_date"] == "2026-09-15T00:00:00Z"


def test_openapi_info_version_matches_distribution():
    assert app.openapi()["info"]["version"] == importlib.metadata.version(DISTRIBUTION)
