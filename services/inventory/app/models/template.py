import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

_driver_fk = f"{_schema}.driver_packages.id" if _schema else "driver_packages.id"
_hypervisor_fk = f"{_schema}.hypervisors.id" if _schema else "hypervisors.id"


class DeviceTemplate(Base):
    __tablename__ = "device_templates"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    template_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="device", server_default="device"
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey(_driver_fk), nullable=True
    )
    # Set only on "dynamic" templates: the hypervisor a materialized instance
    # is created on. Nullable; enforced present for dynamic templates and absent
    # otherwise at the schema and service layers.
    hypervisor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey(_hypervisor_fk), nullable=True
    )
    exclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    icon: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor: Mapped[str] = mapped_column(String(255), nullable=False, default="unknown")
    model: Mapped[str] = mapped_column(String(255), nullable=False, default="unknown")
    part_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sections: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    driver = relationship("DriverPackage", lazy="joined")
