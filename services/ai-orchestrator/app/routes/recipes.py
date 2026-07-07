"""Admin-only recipe-drafting endpoints (ADR 0005, issue #28, phase 2).

Triple gate on every route, checked in order: the ai_recipe_authoring_enabled
flag (403, enforced at the boundary like the write tools, not just absent
from docs), admin role, and for the drafting routes ai_is_configured() (503,
pinned detail). Drafting is quota-enforced and every attempt's tokens are
metered through ai_usage. Nothing here uploads a driver; the response carries
the assembled archive base64-encoded for the admin's explicit upload through
inventory's existing admin endpoint.
"""

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from herd_common.auth import make_auth_dependencies
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.recipe_draft import RecipeDraft
from app.services import usage_repo
from app.services.ai_client import (
    AI_NOT_CONFIGURED_DETAIL,
    AI_PROVIDER_UNREACHABLE_DETAIL,
    AIClient,
    AIError,
    AIProviderUnavailableError,
    ai_is_configured,
    get_ai_client,
)
from app.services.recipe_author import (
    RecipeAuthorError,
    assemble_package_b64,
    author_recipe,
)

logger = logging.getLogger(__name__)

RECIPE_AUTHORING_DISABLED_DETAIL = "AI recipe authoring is disabled"

_get_current_user, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])


def require_recipe_authoring() -> None:
    """Boundary enforcement of the default-off flag (the issue #113 discipline):
    the routes are refused, not merely undocumented, when the flag is off."""
    if not settings.ai_recipe_authoring_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, RECIPE_AUTHORING_DISABLED_DETAIL)


class DraftRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    hypervisor_type: str | None = Field(default=None, max_length=200)


class RefineRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=20000)


class DraftResponse(BaseModel):
    draft_id: uuid.UUID
    valid: bool
    attempts: int
    model: str | None
    prompt: str
    hypervisor_type: str | None
    explanation: str | None
    driver_py: str
    driver_metadata: dict
    validation: dict | None
    package_b64: str
    created_at: datetime
    updated_at: datetime


def _to_response(draft: RecipeDraft) -> DraftResponse:
    metadata = json.loads(draft.driver_metadata_json)
    return DraftResponse(
        draft_id=draft.id,
        valid=draft.valid,
        attempts=draft.attempts,
        model=draft.model,
        prompt=draft.prompt,
        hypervisor_type=draft.hypervisor_type,
        explanation=draft.explanation,
        driver_py=draft.driver_py,
        driver_metadata=metadata,
        validation=json.loads(draft.validation_json) if draft.validation_json else None,
        package_b64=assemble_package_b64(draft.driver_py, metadata),
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


async def _get_draft_or_404(db: AsyncSession, draft_id: uuid.UUID) -> RecipeDraft:
    draft = await db.get(RecipeDraft, draft_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recipe draft not found")
    return draft


async def _run_authoring(
    ai: AIClient,
    db: AsyncSession,
    *,
    user: dict,
    prompt: str,
    hypervisor_type: str | None,
    existing: RecipeDraft | None = None,
    admin_feedback: str = "",
) -> DraftResponse:
    if not ai_is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, AI_NOT_CONFIGURED_DETAIL)

    user_id = uuid.UUID(user["sub"])
    await usage_repo.enforce_quota(db, user_id)

    try:
        draft, usage = await author_recipe(
            ai,
            db,
            user_id=user_id,
            prompt=prompt,
            hypervisor_type=hypervisor_type,
            existing=existing,
            admin_feedback=admin_feedback,
        )
    except RecipeAuthorError as e:
        raise HTTPException(e.status_code, e.message) from e
    except AIProviderUnavailableError as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, AI_PROVIDER_UNREACHABLE_DETAIL
        ) from e
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    await usage_repo.record_usage(db, user_id, usage, fallback_text=prompt + draft.driver_py)
    return _to_response(draft)


@router.post("/draft", response_model=DraftResponse)
async def create_draft(
    body: DraftRequest,
    _flag: None = Depends(require_recipe_authoring),
    admin=Depends(require_admin),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
) -> DraftResponse:
    return await _run_authoring(
        ai,
        db,
        user=admin,
        prompt=body.prompt,
        hypervisor_type=body.hypervisor_type,
    )


@router.post("/draft/{draft_id}/refine", response_model=DraftResponse)
async def refine_draft(
    draft_id: uuid.UUID,
    body: RefineRequest,
    _flag: None = Depends(require_recipe_authoring),
    admin=Depends(require_admin),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
) -> DraftResponse:
    draft = await _get_draft_or_404(db, draft_id)
    return await _run_authoring(
        ai,
        db,
        user=admin,
        prompt=draft.prompt,
        hypervisor_type=draft.hypervisor_type,
        existing=draft,
        admin_feedback=body.feedback,
    )


@router.get("/draft/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: uuid.UUID,
    _flag: None = Depends(require_recipe_authoring),
    admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DraftResponse:
    draft = await _get_draft_or_404(db, draft_id)
    return _to_response(draft)
