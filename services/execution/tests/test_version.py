"""Unit test for GET /version (issue #846).

No DB setup needed: /version, like /health, is unauthenticated and touches
no database. Registered unconditionally in app/main.py, outside
mount_api_routers, so it answers in EXECUTION_POLLER_ONLY mode too; that mode
is exercised separately by the poller-only tests, not here.
"""

import importlib.metadata

import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient

DISTRIBUTION = "herd-execution"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_version(client):
    resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "execution"
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


def test_version_route_not_part_of_mount_api_routers(monkeypatch):
    """/version is registered on the module-level app right beside the bare
    /health route, not inside mount_api_routers, so a poller-only replica
    (which skips mount_api_routers entirely) still serves it. This asserts
    the mounting function itself adds no /version path, mirroring the
    poller-only test in test_health_scheduler_scale.py.
    """
    from app import main as main_module
    from fastapi import FastAPI

    monkeypatch.setattr(main_module.settings, "execution_poller_only", True)
    fresh_app = FastAPI()
    main_module.mount_api_routers(fresh_app)
    paths = {route.path for route in fresh_app.routes}
    assert "/version" not in paths
