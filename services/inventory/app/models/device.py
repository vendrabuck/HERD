import enum
import uuid
from datetime import datetime

from herd_common.enums import TopologyType
from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

_template_fk = f"{_schema}.device_templates.id" if _schema else "device_templates.id"
_config_version_fk = (
    f"{_schema}.device_config_versions.id" if _schema else "device_config_versions.id"
)


class DeviceStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    OFFLINE = "OFFLINE"
    MAINTENANCE = "MAINTENANCE"


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_template_fk),
        nullable=False,
    )
    topology_type: Mapped[TopologyType] = mapped_column(
        Enum(TopologyType, schema=_schema), nullable=False
    )
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, schema=_schema),
        nullable=False,
        default=DeviceStatus.AVAILABLE,
    )
    field_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Idempotency key for the internal dynamic-instance create (issue #275). When
    # execution redelivers a provision_requested event it re-posts the same
    # request_id, so a unique constraint plus return-existing semantics converge
    # on one device row rather than orphaning a duplicate. Null for every
    # admin-created device; Postgres and SQLite both allow multiple NULLs in a
    # unique column, so those rows are unaffected.
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    modified_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            _config_version_fk,
            ondelete="SET NULL",
            use_alter=True,
            name="fk_device_current_config_version",
        ),
        nullable=True,
    )
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    template = relationship("DeviceTemplate", lazy="joined")
    ports = relationship(
        "Port", back_populates="device", lazy="select", cascade="all, delete-orphan"
    )
