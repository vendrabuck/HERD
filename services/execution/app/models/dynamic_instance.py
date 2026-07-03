import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class DynamicInstance(Base):
    """Applied-state ledger for one dynamically-materialized instance (ADR 0004).

    The direct peer of VlanAssignment and RouteAssignment: the create flow
    inserts a CREATING row keyed by the booking's request_id, flips it to ACTIVE
    once the recipe's create_instance succeeds and the instance is materialized
    as an inventory device, and the teardown flow flips it to DESTROYED after
    destroy_instance plus the device delete. Teardown drives strictly from these
    rows, so an ACTIVE row means "the instance may still exist hypervisor-side".

    request_id is unique so a NATS redelivery of reservation.provision_requested
    reuses the same row instead of double-creating an instance; recipes are also
    required to be deterministic on HERD_request_id so a mid-flight retry names
    the same hypervisor-side resource.
    """

    __tablename__ = "dynamic_instances"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, unique=True, index=True
    )
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    hypervisor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Null until the instance is materialized as an inventory device.
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # The hypervisor-side identity (e.g. a VM id) returned by create_instance.
    instance_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CREATING")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
