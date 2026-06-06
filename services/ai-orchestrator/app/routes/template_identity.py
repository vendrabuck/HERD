"""POST /api/ai/templates/suggest-identity: admin-only hardware identity suggest.

Single forced tool_use call to the AI model; returns vendor, model,
optional part_number, confidence, and reasoning. Feature-gated by
ANTHROPIC_API_KEY: 503 when blank. Used by the template editor's
"Suggest with AI" button.
"""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from herd_common.auth import make_auth_dependencies
from pydantic import BaseModel, Field

from app.config import settings
from app.services.ai_client import AIClient, AIError, ai_is_configured, get_ai_client

logger = logging.getLogger(__name__)

_get_current_user, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/templates", tags=["templates"])


class SuggestIdentityRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    sections: list[dict[str, Any]] | None = None


class SuggestIdentityResponse(BaseModel):
    vendor: str
    model: str
    part_number: str | None = None
    confidence: Literal["low", "medium", "high"]
    reasoning: str


@router.post("/suggest-identity", response_model=SuggestIdentityResponse)
async def suggest_identity(
    body: SuggestIdentityRequest,
    _admin=Depends(require_admin),
    ai: AIClient = Depends(get_ai_client),
) -> SuggestIdentityResponse:
    if not ai_is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI orchestrator is not configured",
        )

    try:
        result = await ai.suggest_template_identity(
            name=body.name,
            description=body.description,
            sections=body.sections,
        )
    except AIError as e:
        logger.warning("ai_template_identity_suggestion_failed: %s", e)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"AI suggestion failed: {e}",
        ) from e

    try:
        return SuggestIdentityResponse(**result)
    except (TypeError, ValueError) as e:
        logger.warning("ai_template_identity_suggestion_malformed: %s", result)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"AI returned a malformed suggestion: {e}",
        ) from e
