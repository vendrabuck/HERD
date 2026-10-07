"""Route tests for /recipes (ADR 0005, issue #28, phase 2).

Pins the triple gate in order (flag 403 with pinned wording even for a
configured admin, admin-only 403, unconfigured 503 with the shared pinned
detail), the happy-path response shape including the downloadable archive
and provenance, refine and GET round trips with 404s, quota enforcement,
usage metering, and the /status recipe_authoring field.
"""

import base64
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.routes.recipes import RECIPE_AUTHORING_DISABLED_DETAIL, RECIPE_DRAFT_AI_FAILED_DETAIL
from app.services.ai_client import AI_NOT_CONFIGURED_DETAIL, AIError, get_ai_client
from app.services.llm_provider import Usage
from app.services.recipe_author import RECIPE_VALIDATOR_UNREACHABLE_DETAIL, RecipeAuthorError
from httpx import ASGITransport, AsyncClient
from jose import jwt

_ADMIN_ID = str(uuid.uuid4())

GOOD_REPORT = {
    "valid": True,
    "structural": {"passed": True, "errors": []},
    "policy": {"passed": True, "errors": []},
    "schema": {"present": False, "schema": None, "error": None},
    "dry_run": {"passed": True, "methods": [], "error": None},
}


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


def _headers(role: str = "admin") -> dict:
    return {"Authorization": f"Bearer {_token(role)}"}


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def recipe_env(monkeypatch):
    # Configured AI and the flag ON by default; individual tests flip these.
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")
    monkeypatch.setattr(config_module.settings, "ai_recipe_authoring_enabled", True)
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _stub_ai(responses=None):
    class StubAI:
        def __init__(self):
            self.calls = []

        async def draft_recipe(self, **kwargs):
            self.calls.append(kwargs)
            return (
                {
                    "driver_py": "class Driver:\n    pass\n",
                    "driver_metadata": {"name": "px", "version": "1.0.0"},
                    "explanation": "does the thing",
                },
                Usage(input_tokens=100, output_tokens=50),
            )

    stub = StubAI()
    app.dependency_overrides[get_ai_client] = lambda: stub
    return stub


def _patch_validator(report=GOOD_REPORT):
    return patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(return_value=report),
    )


BODY = {"prompt": "draft a proxmox clone recipe"}


# --- gating ---


async def test_flag_off_403_pinned_even_for_configured_admin(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_recipe_authoring_enabled", False)
    _stub_ai()
    async with async_client as client:
        for method, url, body in [
            ("POST", "/recipes/draft", BODY),
            ("POST", f"/recipes/draft/{uuid.uuid4()}/refine", {"feedback": "x"}),
            ("GET", f"/recipes/draft/{uuid.uuid4()}", None),
        ]:
            resp = await client.request(method, url, json=body, headers=_headers("admin"))
            assert resp.status_code == 403, url
            assert resp.json()["detail"] == RECIPE_AUTHORING_DISABLED_DETAIL, url


async def test_non_admin_403(async_client):
    _stub_ai()
    async with async_client as client:
        resp = await client.post("/recipes/draft", json=BODY, headers=_headers("user"))
    assert resp.status_code == 403


async def test_unconfigured_503_pinned(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    async with async_client as client:
        resp = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL


# --- provider failure (issue #713) ---


async def test_draft_502_on_ai_error_never_leaks_provider_text(async_client, caplog):
    """AIError from the drafting call maps to 502 with the pinned detail; the
    provider's text is logged with traceback, never returned."""
    secret = "upstream 500: body mentions host=db-internal user=herd"

    class RaisingAI:
        async def draft_recipe(self, **kwargs):
            raise AIError(secret)

    app.dependency_overrides[get_ai_client] = lambda: RaisingAI()
    with caplog.at_level("ERROR", logger="app.routes.recipes"):
        async with async_client as client:
            resp = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert resp.status_code == 502
    assert resp.json()["detail"] == RECIPE_DRAFT_AI_FAILED_DETAIL
    assert "db-internal" not in resp.text
    logged = [r for r in caplog.records if r.getMessage() == "ai_recipe_drafting_failed"]
    assert len(logged) == 1
    assert logged[0].exc_info is not None
    assert str(logged[0].exc_info[1]) == secret


# --- draft happy path ---


async def test_draft_happy_path_shape_and_provenance(async_client):
    _stub_ai()
    with _patch_validator():
        async with async_client as client:
            resp = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["valid"] is True
    assert data["attempts"] == 1
    assert data["driver_py"].startswith("class Driver")
    assert data["explanation"] == "does the thing"
    assert data["validation"]["valid"] is True

    metadata = data["driver_metadata"]
    assert metadata["connection_type"] == "Hypervisor"
    assert metadata["supports_dry_run"] is True
    assert metadata["draft_id"] == data["draft_id"]

    # The archive is downloadable and matches the stored files.
    raw = base64.b64decode(data["package_b64"])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert sorted(zf.namelist()) == ["driver.py", "driver_metadata.json"]
        assert json.loads(zf.read("driver_metadata.json"))["draft_id"] == data["draft_id"]


async def test_validator_unreachable_maps_to_503(async_client):
    _stub_ai()
    with patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(side_effect=RecipeAuthorError(503, RECIPE_VALIDATOR_UNREACHABLE_DETAIL)),
    ):
        async with async_client as client:
            resp = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert resp.status_code == 503
    assert resp.json()["detail"] == RECIPE_VALIDATOR_UNREACHABLE_DETAIL


# --- refine and get ---


async def test_refine_and_get_round_trip(async_client):
    _stub_ai()
    with _patch_validator():
        async with async_client as client:
            created = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
            draft_id = created.json()["draft_id"]

            refined = await client.post(
                f"/recipes/draft/{draft_id}/refine",
                json={"feedback": "add TLS verification"},
                headers=_headers("admin"),
            )
            assert refined.status_code == 200, refined.text
            assert refined.json()["draft_id"] == draft_id
            assert refined.json()["attempts"] == 2

            fetched = await client.get(f"/recipes/draft/{draft_id}", headers=_headers("admin"))
            assert fetched.status_code == 200
            assert fetched.json()["driver_metadata"]["draft_id"] == draft_id


async def test_refine_and_get_404_for_unknown_draft(async_client):
    _stub_ai()
    async with async_client as client:
        missing = uuid.uuid4()
        resp = await client.get(f"/recipes/draft/{missing}", headers=_headers("admin"))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Recipe draft not found"
        resp = await client.post(
            f"/recipes/draft/{missing}/refine",
            json={"feedback": "x"},
            headers=_headers("admin"),
        )
        assert resp.status_code == 404


# --- quota and usage metering ---


async def test_usage_recorded_and_quota_enforced(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 120)
    _stub_ai()
    with _patch_validator():
        async with async_client as client:
            first = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
            assert first.status_code == 200
            # First call recorded 150 tokens (100 in + 50 out), which now
            # meets/exceeds the 120 quota, so the next call is rejected
            # before the provider runs.
            second = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert second.status_code == 429


# --- status ---


async def test_status_reports_recipe_authoring_flag(async_client, monkeypatch):
    async with async_client as client:
        on = await client.get("/status")
        assert on.json()["recipe_authoring"] is True
        monkeypatch.setattr(config_module.settings, "ai_recipe_authoring_enabled", False)
        off = await client.get("/status")
        assert off.json()["recipe_authoring"] is False


# --- failed runs are metered (issue #1034) ---


async def _today_total() -> int:
    from app.services import usage_repo
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        return await usage_repo.get_today_total(db, uuid.UUID(_ADMIN_ID))


async def test_failed_draft_validator_unreachable_meters_the_attempt(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    _stub_ai()
    with patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(side_effect=RecipeAuthorError(503, RECIPE_VALIDATOR_UNREACHABLE_DETAIL)),
    ):
        async with async_client as client:
            resp = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert resp.status_code == 503
    # One attempt reached the provider: Usage(100, 50).
    assert await _today_total() == 150


async def test_failed_draft_ai_error_on_a_later_attempt_meters_earlier_attempts(
    async_client, monkeypatch
):
    """Attempt one answered (and failed validation); attempt two's provider
    call raised. Attempt one's tokens are still metered."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    stub = _stub_ai()
    real_draft = stub.draft_recipe

    async def draft_then_fail(**kwargs):
        if stub.calls:
            raise AIError("provider 500")
        return await real_draft(**kwargs)

    stub.draft_recipe = draft_then_fail
    red = {**GOOD_REPORT, "valid": False}
    with _patch_validator(red):
        async with async_client as client:
            resp = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
    assert resp.status_code == 502
    assert await _today_total() == 150


# --- issue #1040: rules with no pin ---

_RECIPE_ROUTES = [
    ("POST", "/recipes/draft", BODY),
    ("POST", f"/recipes/draft/{uuid.uuid4()}/refine", {"feedback": "x"}),
    ("GET", f"/recipes/draft/{uuid.uuid4()}", None),
]


async def test_flag_is_checked_before_authentication(async_client, monkeypatch):
    """AI-RECIPE-3: the flag dependency is declared before require_admin, so
    with the flag off a caller with NO token gets the pinned 403, and with
    the flag on the same caller gets 401."""
    _stub_ai()
    async with async_client as client:
        monkeypatch.setattr(config_module.settings, "ai_recipe_authoring_enabled", False)
        for method, url, body in _RECIPE_ROUTES:
            resp = await client.request(method, url, json=body)
            assert resp.status_code == 403, url
            assert resp.json()["detail"] == RECIPE_AUTHORING_DISABLED_DETAIL, url

        monkeypatch.setattr(config_module.settings, "ai_recipe_authoring_enabled", True)
        for method, url, body in _RECIPE_ROUTES:
            resp = await client.request(method, url, json=body)
            assert resp.status_code == 401, url


def _other_admin_headers() -> dict:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(
        payload, config_module.settings.secret_key, algorithm=config_module.settings.algorithm
    )
    return {"Authorization": f"Bearer {token}"}


async def test_any_admin_may_read_and_refine_another_admins_draft(async_client):
    """AI-RECIPE-11: drafts are admin-scoped working artifacts with no owner
    filter; a second admin reads and refines the first admin's draft."""
    _stub_ai()
    with _patch_validator():
        async with async_client as client:
            created = await client.post("/recipes/draft", json=BODY, headers=_headers("admin"))
            assert created.status_code == 200, created.text
            draft_id = created.json()["draft_id"]

            other = _other_admin_headers()
            fetched = await client.get(f"/recipes/draft/{draft_id}", headers=other)
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["draft_id"] == draft_id

            refined = await client.post(
                f"/recipes/draft/{draft_id}/refine", json={"feedback": "tighten"}, headers=other
            )
    assert refined.status_code == 200, refined.text
    assert refined.json()["draft_id"] == draft_id
    assert refined.json()["attempts"] == 2
