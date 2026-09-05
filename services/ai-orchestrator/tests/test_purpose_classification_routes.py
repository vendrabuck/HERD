"""Route tests for /classify-purpose/preview and /internal/classify-purpose
(issue #646 phase 2, ADR 0013 points 8-11).

Pins the boundary gate in order (flag 403 with the pinned wording, then
503 unconfigured), the happy-path response shape for both passes
(including the forced classify_purpose tool call and signals_used), usage
metering, the 502 wording when the classifier never returns a usable
distribution, and (issue #709) the request bounds plus the rule that the
provider and quota gates run BEFORE the signal gather.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.routes.purpose_classification import PURPOSE_CLASSIFICATION_DISABLED_DETAIL
from app.schemas.purpose import (
    CATEGORIES_MAX_LENGTH,
    DEVICE_IDS_MAX_LENGTH,
    DYNAMIC_REQUESTS_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
)
from app.services import purpose_signals, usage_repo
from app.services.ai_client import AI_NOT_CONFIGURED_DETAIL, get_ai_client
from app.services.llm_provider import Usage
from app.services.purpose_classifier import NO_USABLE_DISTRIBUTION_DETAIL
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker

_TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_USER_ID = str(uuid.uuid4())

PREVIEW_BODY = {"categories": ["qa_regression", "other"], "purpose": "smoke test"}
INTERNAL_BODY = {
    "reservation_id": str(uuid.uuid4()),
    "categories": ["qa_regression", "other"],
    "purpose": "smoke test",
    "user_id": _USER_ID,
    "device_ids": [],
    "start_time": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
    "end_time": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=2)).isoformat(),
    "status": "COMPLETED",
}


def _token() -> str:
    payload = {
        "sub": _USER_ID,
        "role": "user",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload, config_module.settings.secret_key, algorithm=config_module.settings.algorithm
    )


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def purpose_env(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")
    monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", True)
    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    # No inventory/cabling/transcript signals in most tests; the stub gatherers
    # below stand in so route tests do not also have to mock httpx.
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _stub_signal_gatherers(monkeypatch):
    async def _preview(**kwargs):
        return "<purpose_text>smoke test</purpose_text>", ["purpose_text"]

    async def _internal(db, **kwargs):
        return "<purpose_text>smoke test</purpose_text>", ["purpose_text"]

    monkeypatch.setattr(purpose_signals, "gather_preview_signals", _preview)
    monkeypatch.setattr(purpose_signals, "gather_internal_signals", _internal)


def _stub_ai(distributions=None):
    """A stub AIClient whose classify_purpose returns each queued response
    in turn (defaulting to a single always-usable distribution)."""

    class StubAI:
        def __init__(self):
            self.calls = []
            self._queue = list(
                distributions
                or [{"distribution": [{"category": "other", "probability": 1.0}], "rationale": "x"}]
            )

        async def classify_purpose(self, *, categories, signals_block):
            self.calls.append({"categories": categories, "signals_block": signals_block})
            return self._queue.pop(0), Usage(input_tokens=100, output_tokens=20)

    stub = StubAI()
    app.dependency_overrides[get_ai_client] = lambda: stub
    return stub


# --- gating ---


@pytest.mark.asyncio
async def test_preview_403_when_flag_off(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", False)
    _stub_ai()
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 403
    assert resp.json()["detail"] == PURPOSE_CLASSIFICATION_DISABLED_DETAIL


@pytest.mark.asyncio
async def test_internal_403_when_flag_off(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", False)
    _stub_ai()
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=INTERNAL_BODY,
            headers={"X-Internal-Token": "internal-secret"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == PURPOSE_CLASSIFICATION_DISABLED_DETAIL


@pytest.mark.asyncio
async def test_preview_403_when_flag_off_even_without_auth(async_client, monkeypatch):
    """The flag gate runs before auth, mirroring recipes.py: a caller with no
    token still gets the pinned disabled detail, not a 401."""
    monkeypatch.setattr(config_module.settings, "ai_purpose_classification_enabled", False)
    _stub_ai()
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY)
    assert resp.status_code == 403
    assert resp.json()["detail"] == PURPOSE_CLASSIFICATION_DISABLED_DETAIL


@pytest.mark.asyncio
async def test_internal_403_wrong_token(async_client):
    _stub_ai()
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=INTERNAL_BODY,
            headers={"X-Internal-Token": "wrong"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


@pytest.mark.asyncio
async def test_preview_503_when_unconfigured(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    _stub_ai()
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL


@pytest.mark.asyncio
async def test_internal_503_when_unconfigured(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    _stub_ai()
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=INTERNAL_BODY,
            headers={"X-Internal-Token": "internal-secret"},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL


@pytest.mark.asyncio
async def test_preview_requires_categories(async_client):
    _stub_ai()
    body = dict(PREVIEW_BODY)
    body["categories"] = []
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=body, headers=_headers())
    assert resp.status_code == 422


# --- happy path ---


@pytest.mark.asyncio
async def test_preview_happy_path_shape_and_forced_tool_call(async_client):
    stub = _stub_ai(
        [{"distribution": [{"category": "qa_regression", "probability": 1.0}], "rationale": "r"}]
    )
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pass"] == "creation"
    assert body["top_category"] == "qa_regression"
    assert body["distribution"][0]["category"] == "qa_regression"
    assert set(d["category"] for d in body["distribution"]) == {"qa_regression", "other"}
    assert body["signals_used"] == ["purpose_text"]
    assert body["model"] == config_module.settings.ai_model
    assert "generated_at" in body

    assert len(stub.calls) == 1
    assert stub.calls[0]["categories"] == ["qa_regression", "other"]


@pytest.mark.asyncio
async def test_internal_happy_path_pass_is_end(async_client):
    _stub_ai([{"distribution": [{"category": "other", "probability": 1.0}], "rationale": "r"}])
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=INTERNAL_BODY,
            headers={"X-Internal-Token": "internal-secret"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pass"] == "end"


@pytest.mark.asyncio
async def test_preview_502_when_no_usable_distribution_after_retry(async_client):
    _stub_ai(
        [
            {"distribution": [], "rationale": "bad"},
            {"distribution": [], "rationale": "still bad"},
        ]
    )
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 502
    assert resp.json()["detail"] == NO_USABLE_DISTRIBUTION_DETAIL


# --- usage metering ---


@pytest.mark.asyncio
async def test_preview_meters_usage(async_client, monkeypatch):
    from app.services import usage_repo

    record_usage_mock = AsyncMock(wraps=usage_repo.record_usage)
    monkeypatch.setattr(usage_repo, "record_usage", record_usage_mock)
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1_000_000)

    _stub_ai([{"distribution": [{"category": "other", "probability": 1.0}], "rationale": "r"}])
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 200, resp.text
    record_usage_mock.assert_awaited_once()
    _, kwargs = record_usage_mock.call_args
    usage_arg = record_usage_mock.call_args.args[2]
    assert usage_arg.input_tokens == 100
    assert usage_arg.output_tokens == 20


# --- request bounds (issue #709) ---


def _oversize(field: str):
    """One-past-the-cap value for each bounded field; the cap constants are
    the schema's own, so a cap change here is a deliberate schema change."""
    if field == "purpose":
        return "x" * (PURPOSE_MAX_LENGTH + 1)
    if field == "device_ids":
        return [str(uuid.uuid4()) for _ in range(DEVICE_IDS_MAX_LENGTH + 1)]
    if field == "dynamic_requests":
        return [
            {"template_id": str(uuid.uuid4()), "count": 1}
            for _ in range(DYNAMIC_REQUESTS_MAX_LENGTH + 1)
        ]
    if field == "categories":
        return [f"c{i}" for i in range(CATEGORIES_MAX_LENGTH + 1)]
    raise AssertionError(field)


_BOUNDED_FIELDS = ["purpose", "device_ids", "dynamic_requests", "categories"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _BOUNDED_FIELDS)
async def test_preview_422_when_field_oversize(async_client, field, monkeypatch):
    gather = AsyncMock()
    monkeypatch.setattr(purpose_signals, "gather_preview_signals", gather)
    _stub_ai()
    body = dict(PREVIEW_BODY)
    body[field] = _oversize(field)
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=body, headers=_headers())
    assert resp.status_code == 422, resp.text
    assert field in str(resp.json()["detail"])
    gather.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _BOUNDED_FIELDS)
async def test_internal_422_when_field_oversize(async_client, field, monkeypatch):
    gather = AsyncMock()
    monkeypatch.setattr(purpose_signals, "gather_internal_signals", gather)
    _stub_ai()
    body = dict(INTERNAL_BODY)
    body[field] = _oversize(field)
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=body,
            headers={"X-Internal-Token": "internal-secret"},
        )
    assert resp.status_code == 422, resp.text
    assert field in str(resp.json()["detail"])
    gather.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_at_cap_is_accepted(async_client, monkeypatch):
    """The caps are inclusive: exactly-at-cap bodies validate. Pins the
    boundary so an off-by-one in the schema cannot hide behind the 422 tests."""
    _stub_ai([{"distribution": [{"category": "c0", "probability": 1.0}], "rationale": "r"}])
    body = dict(PREVIEW_BODY)
    body["purpose"] = "x" * PURPOSE_MAX_LENGTH
    body["device_ids"] = [str(uuid.uuid4()) for _ in range(DEVICE_IDS_MAX_LENGTH)]
    body["dynamic_requests"] = [
        {"template_id": str(uuid.uuid4()), "count": 1} for _ in range(DYNAMIC_REQUESTS_MAX_LENGTH)
    ]
    body["categories"] = [f"c{i}" for i in range(CATEGORIES_MAX_LENGTH)]
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=body, headers=_headers())
    assert resp.status_code == 200, resp.text


# --- gate ordering: provider and quota before the gather (issue #709) ---


async def _put_user_over_quota(monkeypatch, user_id: str) -> None:
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    async with _TestSessionLocal() as db:
        await usage_repo.record_usage(db, uuid.UUID(user_id), Usage(input_tokens=150))


@pytest.mark.asyncio
async def test_preview_over_quota_429_never_awaits_gatherer(async_client, monkeypatch):
    await _put_user_over_quota(monkeypatch, _USER_ID)
    gather = AsyncMock()
    monkeypatch.setattr(purpose_signals, "gather_preview_signals", gather)
    stub = _stub_ai()
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["remaining"] == 0
    gather.assert_not_awaited()
    assert stub.calls == []


@pytest.mark.asyncio
async def test_internal_over_quota_429_never_awaits_gatherer(async_client, monkeypatch):
    await _put_user_over_quota(monkeypatch, INTERNAL_BODY["user_id"])
    gather = AsyncMock()
    monkeypatch.setattr(purpose_signals, "gather_internal_signals", gather)
    stub = _stub_ai()
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=INTERNAL_BODY,
            headers={"X-Internal-Token": "internal-secret"},
        )
    assert resp.status_code == 429, resp.text
    gather.assert_not_awaited()
    assert stub.calls == []


@pytest.mark.asyncio
async def test_preview_unconfigured_503_never_awaits_gatherer(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    gather = AsyncMock()
    monkeypatch.setattr(purpose_signals, "gather_preview_signals", gather)
    _stub_ai()
    async with async_client as client:
        resp = await client.post("/classify-purpose/preview", json=PREVIEW_BODY, headers=_headers())
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL
    gather.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_unconfigured_503_never_awaits_gatherer(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    gather = AsyncMock()
    monkeypatch.setattr(purpose_signals, "gather_internal_signals", gather)
    _stub_ai()
    async with async_client as client:
        resp = await client.post(
            "/internal/classify-purpose",
            json=INTERNAL_BODY,
            headers={"X-Internal-Token": "internal-secret"},
        )
    assert resp.status_code == 503
    gather.assert_not_awaited()
