import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_runs_fk = f"{_schema}.execution_runs.id" if _schema else "execution_runs.id"


class DeviceHealthStatus(Base):
    __tablename__ = "device_health_status"
    __table_args__ = {"schema": _schema} if _schema else {}

    device_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNKNOWN", server_default="UNKNOWN"
    )
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey(_runs_fk, ondelete="SET NULL"), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    # Polling tier (issue #24): "idle" or "in_use", flipped by consumed
    # reservation lifecycle events. Persisted (not derived at poll time)
    # because the events are acked exactly once and never replay, so an
    # in-memory tier would be lost on a service restart.
    poll_tier: Mapped[str] = mapped_column(
        String(10), nullable=False, default="idle", server_default="idle"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
