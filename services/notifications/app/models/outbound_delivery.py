import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class OutboundDelivery(Base):
    """Idempotency ledger for outbound channels (email, chat, webhook).

    The in-app channel dedupes on the `notifications` table's partial-unique
    index, but outbound channels have no persisted row to collide on. This
    table records that an outbound send already happened for a given
    (channel, user_id, dedupe_key), so a NATS redelivery of the same source
    message is suppressed before the email/chat/webhook side effect fires.

    A row is inserted and committed BEFORE the side effect (reserve-then-send),
    and `_release` deletes it if the send then fails so a redelivery retries.
    This makes outbound delivery at-most-once: a crash or task cancellation in
    the window after the commit and before the release leaves the row and
    permanently suppresses the notification on retry. Rows with a null
    dedupe_key are never written; such messages are not event-sourced and are
    not deduplicated. See issue #83 for the at-least-once alternative
    (a pending/delivered status column).
    """

    __tablename__ = "outbound_deliveries"
    __table_args__ = (
        Index(
            "uq_outbound_channel_user_dedupe",
            "channel",
            "user_id",
            "dedupe_key",
            unique=True,
            sqlite_where=text("dedupe_key IS NOT NULL"),
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
