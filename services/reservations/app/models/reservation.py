import enum
import uuid
from datetime import datetime

from herd_common.enums import TopologyType
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Coerce a device id to UUID so the proxy tolerates str or UUID inputs.

    The `device_id` column is a real Uuid (unlike the old JSON column that
    stored bare strings), and the SQLite backend requires a uuid.UUID instance.
    """
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class ReservationStatus(str, enum.Enum):
    PENDING = "PENDING"
    PENDING_PROVISION = "PENDING_PROVISION"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    owner_name: Mapped[str] = mapped_column(
        String(150), nullable=True, default="", server_default=""
    )
    topology_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    topology_type: Mapped[TopologyType] = mapped_column(
        Enum(TopologyType, schema=_schema), nullable=False
    )
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, schema=_schema),
        nullable=False,
        default=ReservationStatus.ACTIVE,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Set only when an admin cancels a reservation they do not own (issue #340).
    # Owner self-cancel leaves this NULL, so a non-null value is the audit record
    # that an admin force-cancelled on the owner's behalf. modified_by stays the
    # generic last-writer field; this is the dedicated cancel-attribution field.
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # ROADMAP #40: timestamp the expiration task stamps when it has emitted the
    # upcoming-expiry reminder for this reservation. Null means "not yet sent".
    # Dedupes the reminder per reservation across expiration ticks so a
    # reservation in the lead window produces exactly one expiring_soon event.
    expiry_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Durable fork-prune marker (issue #462): device ids (JSON list of UUID strings)
    # removed from this ACTIVE reservation whose fork wiring release has not yet
    # converged. Written in the SAME transaction as the device-set edit, cleared per
    # id when cabling's prune-devices call succeeds, and retried each tick by the
    # expiration sweep's pending-prune reconciler while it is non-null. Null means
    # nothing pending.
    pending_fork_prune_device_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Devices live in the `reservation_devices` join table (single source of truth).
    # selectin eager-loads them with the parent so reading `device_ids` during async
    # response serialization, NATS payloads, reporting, and the expiration task never
    # triggers a lazy load (MissingGreenlet). delete-orphan gives "replace the whole
    # set" semantics when `device_ids` is reassigned. Device order is not preserved.
    devices: Mapped[list["ReservationDevice"]] = relationship(
        "ReservationDevice",
        back_populates="reservation",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    device_ids: AssociationProxy[list[uuid.UUID]] = association_proxy(
        "devices",
        "device_id",
        creator=lambda device_id: ReservationDevice(device_id=_as_uuid(device_id)),
    )

    # Dynamic instance requests (ADR 0004, issue #32). selectin for the same
    # MissingGreenlet reason as devices: the provision_requested payload and the
    # expiration task's backstop read these outside a lazy-load context.
    dynamic_requests: Mapped[list["ReservationDynamicRequest"]] = relationship(
        "ReservationDynamicRequest",
        back_populates="reservation",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ReservationDevice(Base):
    """One device membership of a reservation.

    `device_id` is a bare UUID, NOT a foreign key: devices are owned by the
    inventory service's database, so reservations cannot FK across the service
    boundary. The leading-column index on `device_id` is what makes conflict
    detection and the per-device internal lookups indexed (it replaces the GIN
    index that migration 0005 put on the old JSON column).
    """

    __tablename__ = "reservation_devices"
    __table_args__ = (
        UniqueConstraint("reservation_id", "device_id"),
        Index("ix_reservation_devices_device_id", "device_id"),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}reservations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    reservation: Mapped["Reservation"] = relationship("Reservation", back_populates="devices")


class ReservationDynamicRequest(Base):
    """One requested dynamic instance of a reservation (ADR 0004, issue #32).

    `id` is the request_id the execution service keys its create idempotency
    on (the dynamic_instances ledger's unique request_id), so it must be stable
    across event redeliveries: it is minted once here at booking time.
    `template_id` is a bare UUID, NOT a foreign key: templates are owned by the
    inventory service's database, validated over HTTP at booking time.
    """

    __tablename__ = "reservation_dynamic_requests"
    __table_args__ = (
        Index("ix_reservation_dynamic_requests_reservation_id", "reservation_id"),
        *([{"schema": _schema}] if _schema else [{}]),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    reservation: Mapped["Reservation"] = relationship(
        "Reservation", back_populates="dynamic_requests"
    )
