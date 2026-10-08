"""Admin-gated CRUD for outbound webhook subscriptions (issue #33, phase 4).

The app is mounted under /api/v1 (Traefik strips the prefix), so these routes
are app-relative /webhooks. Only admins manage subscriptions; the NATS consumer
(app.services.nats_consumer) is what actually delivers events.
"""

import asyncio
import json
import logging
import secrets
import uuid
from collections import OrderedDict

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
from app.services.destination import TARGET_NOT_PUBLIC_DETAIL, destination_allowed, target_host

logger = logging.getLogger(__name__)

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
    can verify signatures; it is never echoed again on list or get.

    The target's host must resolve to public addresses only, or be admitted by
    WEBHOOK_ALLOWED_HOSTS (app/services/destination.py); otherwise 422 and
    nothing is stored."""
    if not await destination_allowed(body.target_url, settings.webhook_allowed_hosts):
        logger.warning(
            "Webhook registration refused: host %r does not resolve to an allowed address",
            target_host(body.target_url),
            extra={"action": "webhook_destination_refused"},
        )
        raise HTTPException(status_code=422, detail=TARGET_NOT_PUBLIC_DETAIL)
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


# Test-only delivery sink. Registered by main.py ONLY when
# settings.webhook_test_sink_enabled is true (set in docker-compose.override.yml,
# never in prod), so this unauthenticated endpoint never exists in production. It
# mirrors the repo's HERD_FAULT_INJECTION seam: a test affordance gated to the
# dev/test stack. The services' /health routes are GET-only (405 on POST), so the
# live webhook delivery test needs an in-network endpoint that returns 2xx to POST.
test_sink_router = APIRouter(prefix="/webhooks", tags=["v1-webhooks-test-sink"])


# Longest the sink will sleep before answering (issue #944), so a stray
# delay_ms can never be used to hang the service.
SINK_MAX_DELAY_MS = 10_000
# Arrivals per payload event_id, bounded so a long-lived dev stack cannot grow
# it without limit. Counts POSTs as they ARRIVE (before any delay), which the
# delivery ledger cannot show: its unique (subscription, event) row hides a
# redelivered fan-out's second POST.
_SINK_HITS_MAX_KEYS = 1000
_sink_hits: "OrderedDict[str, int]" = OrderedDict()


def _record_sink_hit(body: bytes) -> None:
    try:
        event_id = json.loads(body).get("event_id")
    except (ValueError, AttributeError):
        return
    if not isinstance(event_id, str):
        return
    _sink_hits[event_id] = _sink_hits.pop(event_id, 0) + 1
    while len(_sink_hits) > _SINK_HITS_MAX_KEYS:
        _sink_hits.popitem(last=False)


@test_sink_router.post("/echo", include_in_schema=False)
async def echo_receiver(request: Request, delay_ms: int = 0):
    """Unauthenticated 200 sink for end-to-end delivery verification (test stack only).

    `delay_ms` (query parameter on the registered target URL, clamped to
    0..SINK_MAX_DELAY_MS) makes the sink answer slowly, so a live test can hold
    a webhook fan-out open past the consumer's ack_wait (issue #944).
    """
    body = await request.body()
    _record_sink_hit(body)
    delay = max(0, min(delay_ms, SINK_MAX_DELAY_MS))
    if delay:
        await asyncio.sleep(delay / 1000)
    return {"ok": True, "received_bytes": len(body)}


@test_sink_router.get("/echo/hits", include_in_schema=False)
async def echo_hits(event_id: str):
    """How many POSTs the sink has received whose JSON body carried `event_id`."""
    return {"event_id": event_id, "count": _sink_hits.get(event_id, 0)}


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
