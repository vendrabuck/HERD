"""Unit tests for app/services/purpose_classifier.py (issue #646 phase 2).

Covers the classify_purpose tool schema, distribution normalization
(unknown-category dropping, negative clamping, missing-category backfill,
rationale capping), and the one-retry-then-502 behavior.
"""

import pytest
from app.services.llm_provider import Usage
from app.services.purpose_classifier import (
    CLASSIFY_PURPOSE_MAX_ATTEMPTS,
    NO_USABLE_DISTRIBUTION_DETAIL,
    PurposeClassifierError,
    build_classify_purpose_tool,
    classify_purpose,
    normalize_distribution,
)

CATEGORIES = ["qa_regression", "feature_development", "other"]


# --- tool schema ---


def test_tool_schema_name_and_enum():
    tool = build_classify_purpose_tool(CATEGORIES)
    assert tool.name == "classify_purpose"
    enum = tool.input_schema["properties"]["distribution"]["items"]["properties"]["category"][
        "enum"
    ]
    assert sorted(enum) == sorted(CATEGORIES)
    assert tool.input_schema["required"] == ["distribution", "rationale"]


def test_tool_schema_dedupes_and_sorts_categories():
    tool = build_classify_purpose_tool(["b", "a", "a", "b"])
    enum = tool.input_schema["properties"]["distribution"]["items"]["properties"]["category"][
        "enum"
    ]
    assert enum == ["a", "b"]


# --- normalization ---


def test_normalize_drops_unknown_categories_and_normalizes():
    raw = {
        "distribution": [
            {"category": "qa_regression", "probability": 0.6},
            {"category": "not_a_real_category", "probability": 0.9},
            {"category": "other", "probability": 0.2},
        ],
        "rationale": "looks like regression testing",
    }
    result = normalize_distribution(raw, CATEGORIES)
    assert result is not None
    cats = {d["category"] for d in result["distribution"]}
    assert cats == set(CATEGORIES)
    total = sum(d["probability"] for d in result["distribution"])
    assert total == pytest.approx(1.0)
    # qa_regression (0.6) should outrank other (0.2) after renormalizing.
    assert result["distribution"][0]["category"] == "qa_regression"


def test_normalize_clamps_negative_probabilities_to_zero():
    raw = {
        "distribution": [
            {"category": "qa_regression", "probability": -0.5},
            {"category": "other", "probability": 1.0},
        ],
        "rationale": "x",
    }
    result = normalize_distribution(raw, CATEGORIES)
    assert result is not None
    by_cat = {d["category"]: d["probability"] for d in result["distribution"]}
    assert by_cat["qa_regression"] == 0.0
    assert by_cat["other"] == pytest.approx(1.0)


def test_normalize_missing_categories_get_zero_and_sort_last():
    raw = {
        "distribution": [{"category": "other", "probability": 1.0}],
        "rationale": "x",
    }
    result = normalize_distribution(raw, CATEGORIES)
    assert result is not None
    dist = result["distribution"]
    assert dist[0]["category"] == "other"
    assert dist[0]["probability"] == pytest.approx(1.0)
    tail_categories = {d["category"] for d in dist[1:]}
    assert tail_categories == {"qa_regression", "feature_development"}
    assert all(d["probability"] == 0.0 for d in dist[1:])


def test_normalize_caps_rationale_length():
    raw = {
        "distribution": [{"category": "other", "probability": 1.0}],
        "rationale": "x" * 1000,
    }
    result = normalize_distribution(raw, CATEGORIES)
    assert result is not None
    assert len(result["rationale"]) == 500


def test_normalize_returns_none_when_no_recognized_category():
    raw = {
        "distribution": [{"category": "not_in_list", "probability": 0.9}],
        "rationale": "x",
    }
    assert normalize_distribution(raw, CATEGORIES) is None


def test_normalize_returns_none_when_all_scores_non_positive():
    raw = {
        "distribution": [
            {"category": "qa_regression", "probability": 0.0},
            {"category": "other", "probability": -1.0},
        ],
        "rationale": "x",
    }
    assert normalize_distribution(raw, CATEGORIES) is None


def test_normalize_returns_none_when_distribution_not_a_list():
    assert normalize_distribution({"distribution": "nonsense"}, CATEGORIES) is None
    assert normalize_distribution({}, CATEGORIES) is None


# --- classify_purpose: retry-then-502 ---


class _StubAI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def classify_purpose(self, *, categories, signals_block):
        self.calls.append({"categories": categories, "signals_block": signals_block})
        raw = self._responses.pop(0)
        return raw, Usage(input_tokens=10, output_tokens=5)


@pytest.mark.asyncio
async def test_classify_purpose_succeeds_on_first_attempt():
    ai = _StubAI([{"distribution": [{"category": "other", "probability": 1.0}], "rationale": "x"}])
    result, usage = await classify_purpose(ai, categories=CATEGORIES, signals_block="<x/>")
    assert len(ai.calls) == 1
    assert result["distribution"][0]["category"] == "other"
    assert usage.input_tokens == 10


@pytest.mark.asyncio
async def test_classify_purpose_retries_once_then_succeeds():
    ai = _StubAI(
        [
            {"distribution": [{"category": "nonsense"}], "rationale": "bad"},
            {"distribution": [{"category": "other", "probability": 1.0}], "rationale": "good"},
        ]
    )
    result, usage = await classify_purpose(ai, categories=CATEGORIES, signals_block="<x/>")
    assert len(ai.calls) == CLASSIFY_PURPOSE_MAX_ATTEMPTS
    assert result["rationale"] == "good"
    # Usage accumulates across both attempts.
    assert usage.input_tokens == 20


@pytest.mark.asyncio
async def test_classify_purpose_raises_pinned_error_after_exhausting_retries():
    ai = _StubAI(
        [
            {"distribution": [], "rationale": "bad"},
            {"distribution": [], "rationale": "still bad"},
        ]
    )
    with pytest.raises(PurposeClassifierError) as exc_info:
        await classify_purpose(ai, categories=CATEGORIES, signals_block="<x/>")
    assert str(exc_info.value) == NO_USABLE_DISTRIBUTION_DETAIL
    assert len(ai.calls) == CLASSIFY_PURPOSE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_classify_purpose_passes_categories_and_signals_through():
    ai = _StubAI([{"distribution": [{"category": "other", "probability": 1.0}], "rationale": "x"}])
    await classify_purpose(
        ai, categories=CATEGORIES, signals_block="<purpose_text>demo</purpose_text>"
    )
    assert ai.calls[0]["categories"] == CATEGORIES
    assert ai.calls[0]["signals_block"] == "<purpose_text>demo</purpose_text>"
