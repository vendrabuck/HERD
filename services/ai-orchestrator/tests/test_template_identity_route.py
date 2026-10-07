"""Tests for POST /api/ai/templates/suggest-identity."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.routes.template_identity import (
    AI_SUGGESTION_FAILED_DETAIL,
    AI_SUGGESTION_MALFORMED_DETAIL,
)
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
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")
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
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
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
    """Pinned detail (issue #713): the provider's message never reaches the
    response body."""
    _override_ai_returning(raises=AIError("model said no: host=db-internal"))
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    body = {"name": "EX4300"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 502
    assert resp.json()["detail"] == AI_SUGGESTION_FAILED_DETAIL
    assert "db-internal" not in resp.text


async def test_suggest_identity_502_when_ai_returns_malformed(async_client):
    _override_ai_returning({"vendor": "Cisco"})  # missing model, confidence, reasoning
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    body = {"name": "X"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json=body, headers=headers)
    assert resp.status_code == 502
    assert resp.json()["detail"] == AI_SUGGESTION_MALFORMED_DETAIL


async def test_suggest_identity_malformed_never_logs_or_returns_model_output(async_client, caplog):
    """Issue #1036 (the #887 rule): the malformed-suggestion branch logs only
    the shape of the failure and answers a fixed detail. The model's invalid
    values reach neither the formatted log line nor the response."""
    from herd_common.logging import JSONFormatter

    secret = "hunter2-model-echoed-credential"
    _override_ai_returning(
        {
            "vendor": f"vendor {secret}",
            "model": "EX4300",
            "confidence": f"certain {secret}",  # not one of low, medium, high
            "reasoning": f"because {secret}",
        }
    )
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    with caplog.at_level("WARNING", logger="app.routes.template_identity"):
        async with async_client as client:
            resp = await client.post(
                "/templates/suggest-identity", json={"name": "X"}, headers=headers
            )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "AI returned a malformed suggestion"
    assert secret not in resp.text

    logged = [
        r for r in caplog.records if r.getMessage() == "ai_template_identity_suggestion_malformed"
    ]
    assert len(logged) == 1
    formatted = JSONFormatter("ai-orchestrator").format(logged[0])
    assert secret not in formatted
    # The shape is there: which field failed and how.
    assert "confidence" in formatted
    assert "literal_error" in formatted


async def test_suggest_identity_ai_error_with_reported_usage_is_metered(async_client, monkeypatch):
    """Issue #1034: an answer without the forced tool call still spent tokens."""
    from app.services import usage_repo
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    _override_ai_returning(raises=AIError("no tool", usage=Usage(input_tokens=6, output_tokens=4)))
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    async with async_client as client:
        resp = await client.post("/templates/suggest-identity", json={"name": "X"}, headers=headers)
    assert resp.status_code == 502
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        assert await usage_repo.get_today_total(db, uuid.UUID(_ADMIN_ID)) == 10
