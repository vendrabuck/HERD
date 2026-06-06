import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

_json_type = JSON().with_variant(JSONB(), "postgresql")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_unread", "user_id", postgresql_where="read_at IS NULL"),
        # Idempotency: one row per (recipient, source event). dedupe_key is the
        # NATS stream+sequence of the originating message, so a redelivery of the
        # same message collapses. Partial-unique (NOT NULL) so rows without a key
        # (legacy or non-event-sourced) are unconstrained.
        Index(
            "uq_notifications_user_dedupe",
            "user_id",
            "dedupe_key",
            unique=True,
            sqlite_where=text("dedupe_key IS NOT NULL"),
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(String(2048), nullable=False)
    data: Mapped[dict] = mapped_column(_json_type, nullable=False, default=dict)
    # NATS stream+sequence of the source message (e.g. "HERD_RESERVATIONS:42").
    # Null for rows not created from a NATS event.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
