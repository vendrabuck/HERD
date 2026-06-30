"""Outbound webhook subscriptions and the delivery ledger (issue #33, phase 4).

An admin registers a WebhookSubscription (target URL, subscribed reservation
event types, shared HMAC secret). The NATS consumer fans reservation lifecycle
events out to every matching active subscription and records each attempt in
WebhookDelivery, which doubles as the at-least-once dedup ledger and the
dead-letter record on retry exhaustion.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

# JSONB on Postgres (queryable, compact), plain JSON on the SQLite unit-test
# backend.
_json_type = JSON().with_variant(JSONB(), "postgresql")


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (*([{"schema": _schema}] if _schema else [{}]),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # List of subject-or-event names, e.g. ["reservation.created","reservation.failed"].
    event_types: Mapped[list] = mapped_column(_json_type, nullable=False, default=list)
    # The shared HMAC secret used to sign deliveries. Stored in plaintext because
    # we must have the cleartext key to compute each signature; encryption-at-rest
    # for this column is tracked separately as issue #39.
    secret: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _fk_target(table: str) -> str:
    """Schema-qualified FK target, so the FK resolves under the integration schema."""
    return f"{_schema}.{table}.id" if _schema else f"{table}.id"


class WebhookDelivery(Base):
    """Per-(subscription, event) delivery ledger and dead-letter record."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # At-least-once dedup: one row per (subscription, source event), so a
        # redelivered NATS event never double-sends to an already-delivered
        # subscription.
        Index(
            "uq_webhook_delivery_subscription_event",
            "subscription_id",
            "event_id",
            unique=True,
        ),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_fk_target("webhook_subscriptions"), ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The stable payload event_id (outbox, issue #21); the delivery idempotency key.
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # "delivered" | "failed" | "dead".
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
