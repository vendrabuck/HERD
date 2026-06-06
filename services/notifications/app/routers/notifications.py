import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from herd_common.auth import make_auth_dependencies
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.notification import (
    MarkAllReadResponse,
    NotificationList,
    NotificationResponse,
    UnreadCount,
)
from app.schemas.preferences import NotificationPreferences, NotificationPreferencesUpdate
from app.services import notification_service

logger = logging.getLogger(__name__)

get_current_user_payload, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

_bearer = HTTPBearer()


def get_bearer_credentials(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> HTTPAuthorizationCredentials:
    """Return the raw bearer credentials after validating the JWT.

    Used by the preferences proxy endpoints to forward the caller's JWT to the
    user-profile service. `get_current_user_payload` discards the raw token
    after verification, so we re-verify here with the raw string on hand.
    """
    try:
        payload = jwt.decode(
            credentials.credentials, settings.secret_key, algorithms=[settings.algorithm]
        )
        if payload.get("sub") is None:
            raise JWTError("missing sub")
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return credentials


router = APIRouter(tags=["notifications"])


def _caller_id(payload: dict) -> uuid.UUID:
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid subject in token",
        )


def _auth_header(credentials: HTTPAuthorizationCredentials) -> dict[str, str]:
    return {"Authorization": f"Bearer {credentials.credentials}"}


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = _caller_id(payload)
    items, total, unread = await notification_service.list_for_user(
        db, user_id, limit=limit, offset=offset, unread_only=unread_only
    )
    return NotificationList(
        items=[NotificationResponse.model_validate(i) for i in items],
        total=total,
        unread=unread,
    )


@router.get("/notifications/unread-count", response_model=UnreadCount)
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    return UnreadCount(count=await notification_service.unread_count(db, _caller_id(payload)))


@router.patch("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    notification = await notification_service.mark_read(db, _caller_id(payload), notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notification


@router.post("/notifications/read-all", response_model=MarkAllReadResponse)
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    updated = await notification_service.mark_all_read(db, _caller_id(payload))
    return MarkAllReadResponse(updated=updated)


@router.delete("/notifications/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    ok = await notification_service.delete_one(db, _caller_id(payload), notification_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")


@router.get("/notifications/preferences", response_model=NotificationPreferences)
async def get_preferences(
    credentials: HTTPAuthorizationCredentials = Depends(get_bearer_credentials),
):
    return await _fetch_prefs_via_jwt(credentials)


@router.put("/notifications/preferences", response_model=NotificationPreferences)
async def put_preferences(
    body: NotificationPreferencesUpdate,
    credentials: HTTPAuthorizationCredentials = Depends(get_bearer_credentials),
    payload: dict = Depends(get_current_user_payload),
):
    current = await _fetch_prefs_via_jwt(credentials)
    new_events = dict(current.events)
    if body.events is not None:
        new_events.update(body.events)
    new_channels = body.channels if body.channels is not None else current.channels
    merged = NotificationPreferences(channels=new_channels, events=new_events)

    url = f"{settings.user_profile_service_url.rstrip('/')}/preferences"
    payload_body = {"extras": {"notifications": merged.model_dump(mode="json")}}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                url,
                headers=_auth_header(credentials),
                json=payload_body,
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        logger.warning("Failed to write preferences", exc_info=True)
        raise HTTPException(status_code=502, detail="user-profile unreachable") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"user-profile error: {resp.status_code}")

    from app.services.preferences_client import get_preferences_client

    get_preferences_client().invalidate(_caller_id(payload))
    return merged


async def _fetch_prefs_via_jwt(
    credentials: HTTPAuthorizationCredentials,
) -> NotificationPreferences:
    url = f"{settings.user_profile_service_url.rstrip('/')}/preferences"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_auth_header(credentials), timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("Failed to read preferences", exc_info=True)
        raise HTTPException(status_code=502, detail="user-profile unreachable") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"user-profile error: {resp.status_code}")
    body = resp.json()
    stored = (body.get("extras") or {}).get("notifications")
    return NotificationPreferences.with_defaults(stored)
