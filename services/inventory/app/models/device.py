import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class DeviceType(str, enum.Enum):
    FIREWALL = "FIREWALL"
    SWITCH = "SWITCH"
    ROUTER = "ROUTER"
    TRAFFIC_SHAPER = "TRAFFIC_SHAPER"
    OTHER = "OTHER"


class TopologyType(str, enum.Enum):
    PHYSICAL = "PHYSICAL"
    CLOUD = "CLOUD"


class DeviceStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    OFFLINE = "OFFLINE"
    MAINTENANCE = "MAINTENANCE"


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    device_type: Mapped[DeviceType] = mapped_column(
        Enum(DeviceType, schema=_schema), nullable=False
    )
    topology_type: Mapped[TopologyType] = mapped_column(
        Enum(TopologyType, schema=_schema), nullable=False
    )
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, schema=_schema),
        nullable=False,
        default=DeviceStatus.AVAILABLE,
    )
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON type works on both SQLite (for tests) and PostgreSQL (JSONB-compatible in prod)
    specs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
