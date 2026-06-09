"""Tests for GET /drivers/{driver_id}/config-schema (issue #23, slice 2).

The internal-token endpoint inventory calls to read a driver's published config
schema. It resolves the cache via load_driver (a SHA256 hit avoids a
re-download), then reads config_schema_json back. Covered:

- 403 without the internal token
- has_schema/schema/source = driver when the cache holds a published schema
- has_schema=False, source=none when the driver published nothing
- fail-open: load_driver failure returns has_schema=False, not 5xx
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.driver_cache import DriverCache
from app.routers.executions import _require_internal_token
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

PUBLISHED_SCHEMA = {
    "type": "object",
    "properties": {"hostname": {"type": "string", "maxLength": 64}},
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


def _override_internal_token():
    return None


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def internal_client():
    app.dependency_overrides[_require_internal_token] = _override_internal_token
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def raw_client():
    """Client WITHOUT the internal-token override, to exercise the 403 path."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _params():
    return {"sha256": "abc", "filename": "driver.zip", "connection_type": "Management"}


@pytest.mark.asyncio
async def test_config_schema_requires_internal_token(raw_client):
    driver_id = uuid.uuid4()
    with patch("app.config.settings.internal_api_token", "real-token"):
        resp = await raw_client.get(
            f"/drivers/{driver_id}/config-schema",
            params=_params(),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_config_schema_returns_published_schema(internal_client, db):
    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="abc",
            local_path="/tmp/driver",
            config_schema_json=json.dumps(PUBLISHED_SCHEMA),
        )
    )
    await db.commit()

    with patch(
        "app.routers.executions.load_driver",
        new=AsyncMock(return_value="/tmp/driver"),
    ):
        resp = await internal_client.get(
            f"/drivers/{driver_id}/config-schema",
            params=_params(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_schema"] is True
    assert body["source"] == "driver"
    assert body["schema"] == PUBLISHED_SCHEMA
    assert body["driver_id"] == str(driver_id)
    assert body["sha256"] == "abc"


@pytest.mark.asyncio
async def test_config_schema_no_published_schema(internal_client, db):
    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="abc",
            local_path="/tmp/driver",
            config_schema_json=None,
        )
    )
    await db.commit()

    with patch(
        "app.routers.executions.load_driver",
        new=AsyncMock(return_value="/tmp/driver"),
    ):
        resp = await internal_client.get(
            f"/drivers/{driver_id}/config-schema",
            params=_params(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_schema"] is False
    assert body["schema"] is None
    assert body["source"] == "none"


@pytest.mark.asyncio
async def test_config_schema_fail_open_on_load_error(internal_client):
    """A load_driver failure must NOT 5xx; inventory falls back to the registry."""
    driver_id = uuid.uuid4()
    with patch(
        "app.routers.executions.load_driver",
        new=AsyncMock(side_effect=RuntimeError("download failed")),
    ):
        resp = await internal_client.get(
            f"/drivers/{driver_id}/config-schema",
            params=_params(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_schema"] is False
    assert body["source"] == "none"
