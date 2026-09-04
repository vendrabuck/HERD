"""POST /classify-purpose/preview and POST /internal/classify-purpose:
lab purpose classification (issue #646 phase 2, ADR 0013 points 8-11).

Both routes are gated by `ai_purpose_classification_enabled` at the route
boundary exactly like `ai_recipe_authoring_enabled` (see
app/routes/recipes.py's `require_recipe_authoring`): the flag dependency is
declared first, so a disabled feature refuses with the pinned 403 detail
before auth or provider configuration are even evaluated. `ai_is_configured()`
then gates with the shared 503, matching every other AI route.

One forced classify_purpose tool call per request, through the LLMProvider
abstraction (app/services/purpose_classifier.py). A signal-fetch failure
(inventory, cabling, or the transcript read) never fails the request: see
app/services/purpose_signals.py, which logs a warning and simply omits that
signal from the response's signals_used list.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from herd_common.auth import make_auth_dependencies
from herd_common.internal_auth import internal_token_matches
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.purpose import (
    InternalClassifyRequest,
    PreviewClassifyRequest,
    PurposeClassification,
)
from app.services import purpose_signals, usage_repo
from app.services.ai_client import (
    AI_NOT_CONFIGURED_DETAIL,
    AI_PROVIDER_UNREACHABLE_DETAIL,
    AIClient,
    AIError,
    AIProviderUnavailableError,
    ai_is_configured,
    get_ai_client,
)
from app.services.purpose_classifier import PurposeClassifierError, classify_purpose

logger = logging.getLogger(__name__)

PURPOSE_CLASSIFICATION_DISABLED_DETAIL = "Purpose classification is disabled"

_get_current_user, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(tags=["purpose-classification"])


def require_purpose_classification() -> None:
    """Boundary enforcement of the default-off flag (the issue #113
    discipline): the routes are refused, not merely undocumented, when the
    flag is off. Mirrors app/routes/recipes.py's require_recipe_authoring."""
    if not settings.ai_purpose_classification_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, PURPOSE_CLASSIFICATION_DISABLED_DETAIL)


async def _bearer_token(authorization: str = Header(...)) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    return authorization.split(" ", 1)[1]


def _check_internal_token(x_internal_token: str) -> None:
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid internal token")


async def _run_classification(
    ai: AIClient,
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    categories: list[str],
    signals_block: str,
    signals_used: list[str],
    classification_pass: str,
) -> PurposeClassification:
    if not ai_is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, AI_NOT_CONFIGURED_DETAIL)

    await usage_repo.enforce_quota(db, user_id)

    try:
        result, usage = await classify_purpose(
            ai, categories=categories, signals_block=signals_block
        )
    except PurposeClassifierError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    except AIProviderUnavailableError as e:
        logger.warning("ai_purpose_classification_provider_unreachable: %s", e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, AI_PROVIDER_UNREACHABLE_DETAIL
        ) from e
    except AIError as e:
        logger.warning("ai_purpose_classification_failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI classification failed: {e}") from e

    await usage_repo.record_usage(db, user_id, usage, fallback_text=signals_block)

    return PurposeClassification(
        distribution=result["distribution"],
        top_category=result["distribution"][0]["category"],
        pass_=classification_pass,
        model=settings.ai_model,
        rationale=result["rationale"],
        generated_at=datetime.now(UTC),
        signals_used=signals_used,
    )


@router.post("/classify-purpose/preview", response_model=PurposeClassification)
async def classify_purpose_preview(
    body: PreviewClassifyRequest,
    _flag: None = Depends(require_purpose_classification),
    user=Depends(_get_current_user),
    token: str = Depends(_bearer_token),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
) -> PurposeClassification:
    signals_block, signals_used = await purpose_signals.gather_preview_signals(
        token=token,
        purpose=body.purpose,
        topology_id=body.topology_id,
        device_ids=body.device_ids,
        dynamic_requests=body.dynamic_requests,
    )
    return await _run_classification(
        ai,
        db,
        user_id=uuid.UUID(user["sub"]),
        categories=body.categories,
        signals_block=signals_block,
        signals_used=signals_used,
        classification_pass="creation",
    )


@router.post("/internal/classify-purpose", response_model=PurposeClassification)
async def classify_purpose_internal(
    body: InternalClassifyRequest,
    _flag: None = Depends(require_purpose_classification),
    x_internal_token: str = Header(...),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
) -> PurposeClassification:
    _check_internal_token(x_internal_token)

    signals_block, signals_used = await purpose_signals.gather_internal_signals(
        db,
        reservation_id=body.reservation_id,
        purpose=body.purpose,
        device_ids=body.device_ids,
        dynamic_requests=body.dynamic_requests,
        start_time=body.start_time,
        end_time=body.end_time,
        status=body.status,
    )
    return await _run_classification(
        ai,
        db,
        user_id=body.user_id,
        categories=body.categories,
        signals_block=signals_block,
        signals_used=signals_used,
        classification_pass="end",
    )
