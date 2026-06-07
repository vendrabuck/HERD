"""Tests for GET /api/ai/quota (the per-user daily token budget read endpoint)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.services import usage_repo
from app.services.llm_provider import Usage
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_USER_ID = str(uuid.uuid4())


def _token(role: str = "user") -> str:
    payload = {
        "sub": _USER_ID,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload,
        config_module.settings.secret_key,
        algorithm=config_module.settings.algorithm,
    )


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


async def test_quota_requires_auth(async_client):
    async with async_client as client:
        resp = await client.get("/quota")
    assert resp.status_code in (401, 403)


async def test_quota_disabled_reports_enabled_false(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 0)
    headers = {"Authorization": f"Bearer {_token()}"}
    async with async_client as client:
        resp = await client.get("/quota", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["limit"] == 0
    assert body["used"] == 0
    assert body["remaining"] == 0
    assert "reset_at" in body


async def test_quota_reports_used_and_remaining(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db, uuid.UUID(_USER_ID), Usage(input_tokens=30, output_tokens=10)
        )
    headers = {"Authorization": f"Bearer {_token()}"}
    async with async_client as client:
        resp = await client.get("/quota", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["limit"] == 100
    assert body["used"] == 40
    assert body["remaining"] == 60


async def test_quota_works_when_over_limit(async_client, monkeypatch):
    """The read endpoint is not gated by enforce_quota: an over-quota user still gets 200."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    async with TestSessionLocal() as db:
        await usage_repo.record_usage(
            db, uuid.UUID(_USER_ID), Usage(input_tokens=150, output_tokens=0)
        )
    headers = {"Authorization": f"Bearer {_token()}"}
    async with async_client as client:
        resp = await client.get("/quota", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["used"] == 150
    assert body["remaining"] == 0
