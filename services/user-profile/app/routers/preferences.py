import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.auth import caller_id as _caller_id
from herd_common.auth import make_auth_dependencies
from herd_common.internal_auth import internal_token_matches
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.preferences import (
    PreferencesPatchRequest,
    PreferencesReplaceRequest,
    PreferencesResponse,
)
from app.services.preferences_service import get_or_create, patch, replace, reset

logger = logging.getLogger(__name__)

get_current_user_payload, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(tags=["preferences"])


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences_endpoint(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    return await get_or_create(db, _caller_id(payload))


@router.put("/preferences", response_model=PreferencesResponse)
async def put_preferences_endpoint(
    body: PreferencesReplaceRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = _caller_id(payload)
    prefs = await replace(db, user_id, body.saved_filters, body.page_sizes, body.extras)
    logger.info(
        "Preferences replaced for user %s",
        user_id,
        extra={"action": "preferences_replace", "user_id": str(user_id)},
    )
    return prefs


@router.patch("/preferences", response_model=PreferencesResponse)
async def patch_preferences_endpoint(
    body: PreferencesPatchRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = _caller_id(payload)
    return await patch(db, user_id, body.saved_filters, body.page_sizes, body.extras)


@router.delete("/preferences", response_model=PreferencesResponse)
async def delete_preferences_endpoint(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = _caller_id(payload)
    prefs = await reset(db, user_id)
    logger.info(
        "Preferences reset for user %s",
        user_id,
        extra={"action": "preferences_reset", "user_id": str(user_id)},
    )
    return prefs


def _require_internal_token(x_internal_token: str | None) -> None:
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal token",
        )


@router.get("/preferences/internal", response_model=PreferencesResponse)
async def get_preferences_internal(
    user_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """Service-to-service read of a user's preferences, guarded by INTERNAL_API_TOKEN.

    Auto-creates the row if missing so callers can rely on a stable shape.
    """
    _require_internal_token(x_internal_token)
    return await get_or_create(db, user_id)
