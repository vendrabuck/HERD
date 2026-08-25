import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

# Partial-unique index: at most one ACTIVE route assignment per
# (reservation, L3 switch). The predicate is ACTIVE-only so RELEASED rows from
# earlier reservations on the same switch never block a new assignment. This is
# the DB-level guard that makes record_route_active idempotent under concurrent NATS
# redelivery: the second insert trips IntegrityError and the service re-reads
# the winner's row, so one reservation can never pin two route sets for one
# switch. Declared here (not only in the migration) so Base.metadata.create_all
# builds it for the SQLite unit-test DB. SQLite and Postgres both honor partial
# unique indexes.
_active_route_unique = Index(
    "uq_route_active_per_res_device",
    "reservation_id",
    "device_id",
    unique=True,
    sqlite_where=text("status = 'ACTIVE'"),
    postgresql_where=text("status = 'ACTIVE'"),
)


class RouteAssignment(Base):
    """The provisioned route set for one L3 switch in one reservation.

    `routes` stores the exact list of {destination, next_hop, interface} dicts
    passed to configure_route at provision time, read from the switch's latest
    inventory config version. Deprovision reads this row back and never
    re-derives from the (possibly edited) config, so remove_route always
    targets exactly what configure_route applied.

    `status` gains "FAILED" as a legal value in ADR 0009 (issue #369/#416): the
    L3 layered reconcile (phase 5) writes it when a fork-adjacency-driven provision
    or removal fails a driver call. `attempts`, `last_error`, and `intended`
    (ACTIVE/RELEASED, mirroring l1_connection_assignments.intended) carry the retry
    bookkeeping the two retry channels split on: a FAILED row intended ACTIVE
    re-provisions the PINNED set, one intended RELEASED re-removes it, always
    verbatim, never re-derived from the switch's current config (issue #20). The
    pin CONTENT (`routes`) is captured once at first provision and reused for the
    life of the pin; phase 5 changes only the row's STATUS lifecycle (driver-gated)
    and WHEN a switch is provisioned/deprovisioned (adjacency gained/lost).
    """

    __tablename__ = "route_assignments"
    __table_args__ = (
        (_active_route_unique, {"schema": _schema}) if _schema else (_active_route_unique,)
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    routes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    intended: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
