"""The classify_purpose forced tool call and response post-processing
(issue #646 phase 2, ADR 0013 points 8 to 11).

Mirrors the shape of recipe_author.py / draft_recipe: a single forced
tool_use call through the LLMProvider abstraction, with a bounded retry
when the model's answer is unusable. This service is deliberately
taxonomy-agnostic (issue #646 refinement 4): `categories` always comes from
the caller, never a fixed enum baked in here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.services.llm_provider import ToolSchema, Usage

if TYPE_CHECKING:
    from app.services.ai_client import AIClient

logger = logging.getLogger(__name__)

NO_USABLE_DISTRIBUTION_DETAIL = "Purpose classifier returned no usable distribution"

# One initial attempt plus one retry, per the fixed contract ("after one retry").
CLASSIFY_PURPOSE_MAX_ATTEMPTS = 2

RATIONALE_CHAR_CAP = 500

CLASSIFY_PURPOSE_SYSTEM_PROMPT = """You classify why a HERD lab reservation exists, choosing \
only from a closed taxonomy the caller supplies. Base your judgment solely on the \
structured signals below (and, when present, the reservation assistant \
transcript); you are never given credentials, secret values, or device \
configuration contents, so do not assume any. Return your answer via the \
classify_purpose tool: a probability distribution over exactly the \
supplied categories (they need not sum to 1.0, the caller normalizes) plus \
a short rationale, at most a couple of sentences, that a lab admin can \
read at a glance."""


class PurposeClassifierError(Exception):
    """Raised when no usable distribution was produced after the retry budget."""


def build_classify_purpose_tool(categories: list[str]) -> ToolSchema:
    enum = sorted(set(categories))
    return ToolSchema(
        name="classify_purpose",
        description=(
            "Classify the reservation's purpose as a probability distribution "
            "over the supplied categories, with a short rationale."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "distribution": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "enum": enum},
                            "probability": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["category", "probability"],
                    },
                },
                "rationale": {"type": "string"},
            },
            "required": ["distribution", "rationale"],
        },
    )


def normalize_distribution(raw: dict[str, Any], categories: list[str]) -> dict[str, Any] | None:
    """Post-process one classify_purpose tool-call result.

    Drops categories outside the supplied list, clamps negative
    probabilities to 0, normalizes the remaining scores to sum to 1.0,
    ensures every supplied category appears (missing ones get 0.0 and sort
    last), and caps the rationale at RATIONALE_CHAR_CAP characters. Returns
    None when there is nothing usable to normalize (no recognized category,
    or every recognized score is non-positive), which the caller treats as
    a retry-then-502 case.
    """
    raw_distribution = raw.get("distribution")
    if not isinstance(raw_distribution, list):
        return None

    known = set(categories)
    scores: dict[str, float] = {}
    for item in raw_distribution:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if category not in known:
            continue
        try:
            probability = float(item.get("probability", 0))
        except (TypeError, ValueError):
            continue
        scores[category] = max(0.0, probability)

    total = sum(scores.values())
    if total <= 0:
        return None

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    distribution = [{"category": c, "probability": v / total} for c, v in ordered]
    present = {c for c, _ in ordered}
    distribution.extend({"category": c, "probability": 0.0} for c in categories if c not in present)

    rationale = str(raw.get("rationale") or "")[:RATIONALE_CHAR_CAP]
    return {"distribution": distribution, "rationale": rationale}


async def classify_purpose(
    ai: "AIClient",
    *,
    categories: list[str],
    signals_block: str,
) -> tuple[dict[str, Any], Usage]:
    """Run the classify_purpose tool call, retrying once on an unusable answer.

    Raises PurposeClassifierError with the pinned detail after
    CLASSIFY_PURPOSE_MAX_ATTEMPTS unusable attempts; any AIError/
    AIProviderUnavailableError from the provider call itself propagates
    unchanged for the route to map.
    """
    total_usage = Usage()
    for attempt in range(1, CLASSIFY_PURPOSE_MAX_ATTEMPTS + 1):
        raw, usage = await ai.classify_purpose(categories=categories, signals_block=signals_block)
        total_usage.add(usage)
        normalized = normalize_distribution(raw, categories)
        if normalized is not None:
            return normalized, total_usage
        logger.warning(
            "ai_purpose_classification_unusable_distribution",
            extra={"attempt": attempt},
        )
    raise PurposeClassifierError(NO_USABLE_DISTRIBUTION_DETAIL)
