"""Per-subscription webhook delivery and the delivery ledger (issue #33).

A delivery is idempotent on (subscription_id, event_id): a redelivered event that
already has a `delivered` ledger row is skipped, so an at-least-once event stream
never double-sends to an already-delivered subscription. A delivery that
exhausts its retries writes a `dead` ledger row, which is the dead-letter record
the acceptance criteria require. Each delivery runs in its own DB session so the
fan-out can run concurrently (a SQLAlchemy async session is not concurrency-safe).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from herd_common.retry import retry_with_backoff
from herd_common.webhooks import WEBHOOK_SIGNATURE_HEADER, sign_body
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.webhook import WebhookDelivery, WebhookSubscription

logger = logging.getLogger(__name__)

# Backoff between retry attempts. Kept short so a dead-letter resolves promptly;
# the per-attempt cap is the caller-supplied timeout.
RETRY_INITIAL_DELAY = 0.5
RETRY_MAX_DELAY = 5.0


@dataclass(frozen=True)
class Target:
    """The immutable subset of a subscription a delivery needs, detached from any
    session so the fan-out can open its own session per delivery."""

    id: object
    target_url: str
    secret: str


async def load_matching_targets(session_factory, event_name: str) -> list[Target]:
    """Return active subscriptions subscribed to `event_name`.

    Matching is done in Python (load active rows, filter by JSON membership) so it
    stays portable across the SQLite unit backend and Postgres; the active set is
    small in practice.
    """
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
    return [
        Target(id=r.id, target_url=r.target_url, secret=r.secret)
        for r in rows
        if event_name in (r.event_types or [])
    ]


async def deliver_one(
    session_factory,
    target: Target,
    body: bytes,
    event_id: str,
    event_type: str,
    *,
    timeout: float,
    attempts: int,
) -> str:
    """Deliver one event to one subscription, recording the outcome.

    Returns "skipped" (already delivered), "delivered", or "dead". Each POST is
    bounded by `timeout`, so a slow receiver cannot stall the other concurrent
    deliveries or the consumer loop. Transient failures (timeouts, connection
    errors, non-2xx) are retried up to `attempts`; on exhaustion a `dead` ledger
    row is written.
    """
    async with session_factory() as session:
        existing = (
            await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.subscription_id == target.id,
                    WebhookDelivery.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == "delivered":
            return "skipped"

        signature = sign_body(body, target.secret)
        headers = {
            "Content-Type": "application/json",
            WEBHOOK_SIGNATURE_HEADER: signature,
        }
        state = {"attempts": 0, "status": None}

        async def _post() -> httpx.Response:
            state["attempts"] += 1
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(target.target_url, content=body, headers=headers)
            state["status"] = resp.status_code
            resp.raise_for_status()
            return resp

        try:
            await retry_with_backoff(
                _post,
                attempts=attempts,
                initial_delay=RETRY_INITIAL_DELAY,
                max_delay=RETRY_MAX_DELAY,
            )
        except Exception as exc:
            return await _record(
                session,
                existing,
                target,
                event_id,
                event_type,
                status="dead",
                attempts=state["attempts"],
                response_status=state["status"],
                last_error=str(exc)[:1024],
            )

        return await _record(
            session,
            existing,
            target,
            event_id,
            event_type,
            status="delivered",
            attempts=state["attempts"],
            response_status=state["status"],
            last_error=None,
            delivered_at=datetime.now(timezone.utc),
        )


async def _record(
    session,
    existing,
    target: Target,
    event_id: str,
    event_type: str,
    *,
    status: str,
    attempts: int,
    response_status,
    last_error,
    delivered_at: datetime | None = None,
) -> str:
    """Insert or update the ledger row for (subscription, event) and commit."""
    row = existing
    if row is None:
        row = WebhookDelivery(subscription_id=target.id, event_id=event_id, event_type=event_type)
        session.add(row)
    row.status = status
    row.attempts = attempts
    row.response_status = response_status
    row.last_error = last_error
    row.delivered_at = delivered_at
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent delivery of the same (subscription, event) already wrote
        # the row. Treat it as handled rather than double-recording.
        await session.rollback()
        logger.info(
            "Webhook delivery row race for subscription %s event %s; already recorded",
            target.id,
            event_id,
        )
    return status
