import enum
import uuid
from datetime import datetime
from typing import Any

from herd_common.enums import TopologyType
from sqlalchemy import JSON, DateTime, Enum, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class ReservationStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    # Stored as JSON list of UUID strings, compatible with SQLite and PostgreSQL
    # In production Postgres, a migration can switch this to UUID[] for native array support
    device_ids: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    topology_type: Mapped[TopologyType] = mapped_column(
        Enum(TopologyType, schema=_schema), nullable=False
    )
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, schema=_schema),
        nullable=False,
        default=ReservationStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
