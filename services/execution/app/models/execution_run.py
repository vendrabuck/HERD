import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    # created_at is indexed for the default `ORDER BY created_at DESC` listing;
    # the composites serve filtered listings and the reporting windowed count
    # (equality filter + created_at ordering) in a single index. See migration
    # 0008. The DESC qualifier is a no-op on SQLite and used by Postgres.
    __table_args__ = (
        Index("ix_execution_runs_created_at", text("created_at DESC")),
        Index("ix_execution_runs_device_created_at", "device_id", text("created_at DESC")),
        Index(
            "ix_execution_runs_reservation_created_at",
            "reservation_id",
            text("created_at DESC"),
        ),
        # Idempotency guard lookup: "has a SUCCESS run for this source message
        # already applied this (device, action, ports)?" keyed on dedupe_key.
        Index(
            "ix_execution_runs_dedupe_device_action",
            "dedupe_key",
            "device_id",
            "action",
        ),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    driver_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    driver_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    input_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    port_a: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port_b: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # NATS "<stream>:<sequence>" of the source reservation event. Stable across
    # redeliveries, so a SUCCESS run keyed on it lets a NAK retry skip the driver
    # actions it already applied (issue #133). Null for non-event runs (e.g. the
    # health scheduler) and when JetStream metadata is unavailable.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
