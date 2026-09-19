"""Unit test for GET /version (issue #846).

Mirrors test_health in test_grants.py: no DB or auth setup needed, since
/version, like /health, is unauthenticated and touches no database.
"""

import importlib.metadata

import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient

DISTRIBUTION = "herd-acl"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_version(client):
    resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "acl"
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


def test_openapi_info_version_matches_distribution():
    assert app.openapi()["info"]["version"] == importlib.metadata.version(DISTRIBUTION)
