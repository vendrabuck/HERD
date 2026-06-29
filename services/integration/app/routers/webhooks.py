"""Admin-gated CRUD for outbound webhook subscriptions (issue #33, phase 4).

The app is mounted under /api/v1 (Traefik strips the prefix), so these routes
are app-relative /webhooks. Only admins manage subscriptions; the NATS consumer
(app.services.nats_consumer) is what actually delivers events.
"""

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from herd_common.auth import make_auth_dependencies
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.schemas.webhook import (
    WebhookCreate,
    WebhookCreated,
    WebhookDeliveryResponse,
    WebhookResponse,
)

_get_current_user_payload, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/webhooks", tags=["v1-webhooks"])


def _principal_id(payload: dict) -> uuid.UUID | None:
    sub = payload.get("sub")
    try:
        return uuid.UUID(str(sub)) if sub else None
    except (ValueError, TypeError):
        return None


@router.post("", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate,
    payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Register an outbound webhook. Returns the secret once so the registrant
    can verify signatures; it is never echoed again on list or get."""
    secret = body.secret or secrets.token_urlsafe(32)
    sub = WebhookSubscription(
        target_url=body.target_url,
        event_types=body.event_types,
        secret=secret,
        description=body.description,
        created_by=_principal_id(payload),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return WebhookCreated(
        id=sub.id,
        target_url=sub.target_url,
        event_types=sub.event_types,
        is_active=sub.is_active,
        description=sub.description,
        created_by=sub.created_by,
        created_at=sub.created_at,
        secret=sub.secret,
    )


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    _payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List subscriptions. Secret omitted for hygiene."""
    rows = (
        (
            await db.execute(
                select(WebhookSubscription).order_by(WebhookSubscription.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [WebhookResponse.model_validate(r) for r in rows]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: uuid.UUID,
    _payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    sub = await db.get(WebhookSubscription, webhook_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return WebhookResponse.model_validate(sub)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    _payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    sub = await db.get(WebhookSubscription, webhook_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await db.delete(sub)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/echo", include_in_schema=False)
async def echo_receiver(request: Request):
    """Unauthenticated 200 sink for end-to-end delivery verification.

    Hidden from the OpenAPI schema (include_in_schema=False) so it stays out of
    the v1 contract and the contract snapshot. It performs no writes; it exists
    so an in-network delivery target reliably returns 2xx for a webhook POST,
    because the services' /health routes are GET-only and would 405. Used by the
    live webhook integration test as the reachable success receiver.
    """
    body = await request.body()
    return {"ok": True, "received_bytes": len(body)}


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryResponse])
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = 100,
    _payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Recent delivery ledger rows for a subscription (newest first)."""
    sub = await db.get(WebhookSubscription, webhook_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    limit = max(1, min(limit, 500))
    rows = (
        (
            await db.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.subscription_id == webhook_id)
                .order_by(WebhookDelivery.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [WebhookDeliveryResponse.model_validate(r) for r in rows]
