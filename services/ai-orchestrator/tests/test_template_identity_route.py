"""Tests for POST /api/ai/templates/suggest-identity."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.services.ai_client import AIError, get_ai_client
from app.services.llm_provider import Usage
from httpx import ASGITransport, AsyncClient
from jose import jwt

_ADMIN_ID = str(uuid.uuid4())


def _token(role: str = "admin") -> str:
    payload = {
        "sub": _ADMIN_ID,
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
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-fake")
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def setup_db():
    # The route opens a DB session for the quota hooks; create the ai_usage
    # table so quota-enabled tests work. Quota is disabled by default, so the
    # hooks are no-ops unless a test sets it.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _override_ai_returning(suggestion: dict | None = None, *, raises: Exception | None = None):
    class StubAI:
        async def suggest_template_identity(self, **kwargs):
            if raises is not None:
                raise raises
            return suggestion, Usage(input_tokens=10, output_tokens=20)

    app.dependency_overrides[get_ai_client] = lambda: StubAI()


async def test_suggest_identity_503_when_key_blank(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    body = {"name": "EX4300"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 503


async def test_suggest_identity_requires_admin(async_client):
    _override_ai_returning(
        {
            "vendor": "Cisco",
            "model": "Catalyst 9300",
            "part_number": None,
            "confidence": "high",
            "reasoning": "Name names a Catalyst SKU.",
        }
    )
    headers = {"Authorization": f"Bearer {_token('user')}"}
    body = {"name": "Catalyst 9300"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 403


async def test_suggest_identity_returns_structured_suggestion(async_client):
    _override_ai_returning(
        {
            "vendor": "Juniper Networks",
            "model": "EX4300",
            "part_number": None,
            "confidence": "high",
            "reasoning": "EX4300 is a Juniper EX Series switch.",
        }
    )
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    body = {"name": "EX4300", "description": "Border firewall"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor"] == "Juniper Networks"
    assert data["model"] == "EX4300"
    assert data["part_number"] is None
    assert data["confidence"] == "high"
    assert data["reasoning"]


async def test_suggest_identity_502_when_ai_fails(async_client):
    _override_ai_returning(raises=AIError("model said no"))
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    body = {"name": "EX4300"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 502


async def test_suggest_identity_502_when_ai_returns_malformed(async_client):
    _override_ai_returning({"vendor": "Cisco"})  # missing model, confidence, reasoning
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    body = {"name": "X"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 502
