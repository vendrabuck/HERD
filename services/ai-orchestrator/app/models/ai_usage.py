"""Per-user daily AI token usage accounting.

One row per (user_id, usage_date) accumulates the input and output tokens a
user has spent across all AI features on that UTC day. The unique index on
(user_id, usage_date) is the conflict target for the atomic upsert in
usage_repo, and the date-only column makes the daily reset implicit: a new UTC
day is simply a new usage_date, hence a fresh row. Rows are only written when a
positive quota is configured (see settings.ai_daily_token_quota); with the
quota disabled the table stays empty.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

# Resolve schema once at import. Empty string (test env: sqlite + DB_SCHEMA="")
# becomes None so the table lands in the default namespace; Postgres uses the
# configured schema. Matches the pattern in app/models/conversation.py.
_schema = settings.db_schema or None


class AIUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("uq_ai_usage_user_date", "user_id", "usage_date", unique=True),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
