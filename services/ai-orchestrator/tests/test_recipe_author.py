"""Unit tests for the recipe drafting loop (ADR 0005, issue #28, phase 2).

Covers the owned-metadata injection (a model cannot weaken connection_type,
supports_dry_run, or provenance), package assembly round trip, validator
client degrade behavior with pinned wording, report-to-feedback flattening,
and the bounded draft-validate-repair loop including refine seeding, usage
accumulation across attempts, and persistence of red drafts.
"""

import base64
import io
import json
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app import config as config_module
from app.database import Base, engine
from app.models.recipe_draft import RecipeDraft
from app.services.llm_provider import Usage
from app.services.recipe_author import (
    RECIPE_VALIDATOR_UNREACHABLE_DETAIL,
    RecipeAuthorError,
    _report_feedback,
    assemble_package_b64,
    author_recipe,
    build_metadata,
    validate_with_execution,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

USER_ID = uuid.uuid4()

GOOD_REPORT = {
    "valid": True,
    "structural": {"passed": True, "errors": []},
    "policy": {"passed": True, "errors": []},
    "schema": {"present": False, "schema": None, "error": None},
    "dry_run": {"passed": True, "methods": [], "error": None},
}

RED_REPORT = {
    "valid": False,
    "structural": {"passed": False, "errors": ["Driver class is missing required method: status"]},
    "policy": {"passed": True, "errors": []},
    "schema": {"present": False, "schema": None, "error": None},
    "dry_run": {"passed": False, "methods": [], "error": "not run"},
}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


class StubAI:
    """Records every draft_recipe call; returns queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def draft_recipe(self, **kwargs):
        self.calls.append(kwargs)
        data = self.responses.pop(0)
        return data, Usage(input_tokens=100, output_tokens=50)


def _draft_data(name="proxmox-clone"):
    return {
        "driver_py": "class Driver:\n    pass\n",
        "driver_metadata": {"name": name, "version": "1.0.0", "notes": "test"},
        "explanation": "clones a template VM",
    }


# --- build_metadata ---


def test_build_metadata_injects_owned_fields():
    draft_id = uuid.uuid4()
    metadata = build_metadata({"name": "px", "version": "2.0", "notes": "n"}, draft_id)
    assert metadata["connection_type"] == "Hypervisor"
    assert metadata["supports_dry_run"] is True
    assert metadata["draft_id"] == str(draft_id)
    assert metadata["generated_by"] == config_module.settings.ai_model
    assert metadata["generated_at"]
    assert metadata["name"] == "px"
    assert metadata["notes"] == "n"


def test_build_metadata_overrides_model_supplied_contract_fields():
    # A model trying to weaken the contract is silently overwritten.
    metadata = build_metadata(
        {"name": "x", "version": "1", "connection_type": "Management", "supports_dry_run": False},
        uuid.uuid4(),
    )
    assert metadata["connection_type"] == "Hypervisor"
    assert metadata["supports_dry_run"] is True


def test_build_metadata_defaults_when_model_omits():
    metadata = build_metadata({}, uuid.uuid4())
    assert metadata["name"] == "generated-recipe"
    assert metadata["version"] == "0.1.0"
    assert "notes" not in metadata


# --- assemble_package_b64 ---


def test_assemble_package_round_trip():
    metadata = build_metadata({"name": "x", "version": "1"}, uuid.uuid4())
    b64 = assemble_package_b64("class Driver: ...\n", metadata)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(b64))) as zf:
        assert sorted(zf.namelist()) == ["driver.py", "driver_metadata.json"]
        assert zf.read("driver.py").decode() == "class Driver: ...\n"
        parsed = json.loads(zf.read("driver_metadata.json").decode())
    assert parsed == metadata


# --- validate_with_execution ---


async def test_validate_posts_with_internal_token(monkeypatch):
    monkeypatch.setattr(config_module.settings, "internal_api_token", "tok")
    ok = httpx.Response(200, json=GOOD_REPORT)
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=ok)
        report = await validate_with_execution("cGtn")
    assert report == GOOD_REPORT
    kwargs = instance.post.await_args.kwargs
    assert kwargs["headers"] == {"X-Internal-Token": "tok"}
    assert kwargs["json"]["connection_type"] == "Hypervisor"


async def test_validate_unreachable_raises_503_pinned():
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(RecipeAuthorError) as exc:
            await validate_with_execution("cGtn")
    assert exc.value.status_code == 503
    assert exc.value.message == RECIPE_VALIDATOR_UNREACHABLE_DETAIL


async def test_validate_non_200_raises_503_pinned():
    with patch("httpx.AsyncClient") as mock_cls:
        instance = mock_cls.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(RecipeAuthorError) as exc:
            await validate_with_execution("cGtn")
    assert exc.value.status_code == 503


# --- _report_feedback ---


def test_report_feedback_flattens_all_sections():
    report = {
        "structural": {"errors": ["missing method"]},
        "policy": {"errors": ["non-stdlib import"]},
        "dry_run": {
            "methods": [
                {"action": "login", "passed": True},
                {"action": "status", "passed": False, "error": "boom"},
                {
                    "action": "create_instance",
                    "passed": False,
                    "error": None,
                    "output": {"success": False, "error": "driver said no"},
                },
            ]
        },
    }
    text = _report_feedback(report)
    assert "structural: missing method" in text
    assert "policy: non-stdlib import" in text
    assert "dry_run: status failed: boom" in text
    assert "dry_run: create_instance failed: driver said no" in text
    assert "login" not in text


# --- author_recipe loop ---


async def test_author_valid_first_attempt_persists(db):
    ai = StubAI([_draft_data()])
    with patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(return_value=GOOD_REPORT),
    ):
        draft, usage = await author_recipe(ai, db, user_id=USER_ID, prompt="make a recipe")

    assert draft.valid is True
    assert draft.attempts == 1
    assert usage.input_tokens == 100 and usage.output_tokens == 50
    metadata = json.loads(draft.driver_metadata_json)
    assert metadata["draft_id"] == str(draft.id)
    assert metadata["generated_by"] == config_module.settings.ai_model
    stored = await db.get(RecipeDraft, draft.id)
    assert stored is not None
    assert stored.user_id == USER_ID
    assert json.loads(stored.validation_json)["valid"] is True


async def test_author_repair_loop_feeds_report_back(db):
    ai = StubAI([_draft_data("v1"), _draft_data("v2")])
    validator = AsyncMock(side_effect=[RED_REPORT, GOOD_REPORT])
    with patch("app.services.recipe_author.validate_with_execution", new=validator):
        draft, usage = await author_recipe(ai, db, user_id=USER_ID, prompt="make a recipe")

    assert draft.valid is True
    assert draft.attempts == 2
    # Usage accumulated across both attempts.
    assert usage.input_tokens == 200 and usage.output_tokens == 100
    # The second call carried the validator's error and the first draft's files.
    second = ai.calls[1]
    assert "missing required method: status" in second["repair_feedback"]
    assert second["previous_driver_py"] == _draft_data()["driver_py"]
    assert json.loads(second["previous_metadata_json"])["name"] == "v1"


async def test_author_exhausts_attempts_and_persists_red_draft(db, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_recipe_max_attempts", 2)
    ai = StubAI([_draft_data(), _draft_data()])
    with patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(return_value=RED_REPORT),
    ):
        draft, usage = await author_recipe(ai, db, user_id=USER_ID, prompt="make a recipe")

    assert draft.valid is False
    assert draft.attempts == 2
    assert len(ai.calls) == 2
    assert json.loads(draft.validation_json)["valid"] is False


async def test_author_refine_updates_in_place_and_seeds_previous(db):
    ai = StubAI([_draft_data("first")])
    with patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(return_value=GOOD_REPORT),
    ):
        first, _ = await author_recipe(ai, db, user_id=USER_ID, prompt="make a recipe")

    ai2 = StubAI([_draft_data("second")])
    with patch(
        "app.services.recipe_author.validate_with_execution",
        new=AsyncMock(return_value=GOOD_REPORT),
    ):
        refined, _ = await author_recipe(
            ai2,
            db,
            user_id=USER_ID,
            prompt=first.prompt,
            existing=first,
            admin_feedback="use the v2 API",
        )

    assert refined.id == first.id
    assert refined.attempts == 2  # 1 from the first session + 1 refine attempt
    assert json.loads(refined.driver_metadata_json)["name"] == "second"
    # The refine call was seeded with the stored files and the admin feedback.
    call = ai2.calls[0]
    assert call["repair_feedback"] == "use the v2 API"
    assert call["previous_driver_py"] == first.driver_py
    # draft_id provenance is stable across refine rounds.
    assert json.loads(refined.driver_metadata_json)["draft_id"] == str(first.id)
