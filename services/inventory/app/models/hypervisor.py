import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_device_group_fk = f"{_schema}.device_groups.id" if _schema else "device_groups.id"


class Hypervisor(Base):
    __tablename__ = "hypervisors"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    hypervisor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Bare UUID into the secrets service; credentials never live inline here.
    # Validated at registration via the secrets internal value endpoint.
    secret_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Read by the booking (reservations) and by execution's create (issue #1033):
    # a template whose hypervisor is disabled is refused at booking and its
    # instances are not created.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # Who may see and book this hypervisor's dynamic templates (issue #1053): the
    # user groups with a permission on this device group. Null means admins only.
    device_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_device_group_fk, ondelete="SET NULL", name="fk_hypervisors_device_group_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
