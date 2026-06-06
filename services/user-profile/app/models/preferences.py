import uuid
from datetime import datetime

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

# Prefer Postgres JSONB in production; JSON for SQLite-backed tests.
_json_type = JSON().with_variant(JSONB(), "postgresql")


class UserPreferences(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (*([{"schema": _schema}] if _schema else [{}]),)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    saved_filters: Mapped[dict] = mapped_column(_json_type, nullable=False, default=dict)
    page_sizes: Mapped[dict] = mapped_column(_json_type, nullable=False, default=dict)
    extras: Mapped[dict] = mapped_column(_json_type, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
