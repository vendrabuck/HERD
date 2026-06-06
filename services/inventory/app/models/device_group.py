import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""


class DeviceGroup(Base):
    __tablename__ = "device_groups"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    devices: Mapped[list["DeviceGroupDevice"]] = relationship(
        "DeviceGroupDevice", back_populates="device_group", cascade="all, delete-orphan"
    )
    permissions: Mapped[list["DeviceGroupPermission"]] = relationship(
        "DeviceGroupPermission", back_populates="device_group", cascade="all, delete-orphan"
    )


class DeviceGroupDevice(Base):
    __tablename__ = "device_group_devices"
    __table_args__ = (
        UniqueConstraint("device_group_id", "device_id"),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    device_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}device_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}devices.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device_group: Mapped["DeviceGroup"] = relationship("DeviceGroup", back_populates="devices")
    device: Mapped["Device"] = relationship("Device")


class DeviceGroupPermission(Base):
    __tablename__ = "device_group_permissions"
    __table_args__ = (
        UniqueConstraint("device_group_id", "user_group_id"),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    device_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}device_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    device_group: Mapped["DeviceGroup"] = relationship("DeviceGroup", back_populates="permissions")


# Import Device for relationship resolution
from app.models.device import Device  # noqa: E402, F401
