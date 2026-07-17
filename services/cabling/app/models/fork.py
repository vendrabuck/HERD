"""Per-reservation editable topology fork (issue #25, fork-on-reserve).

These three tables hold the editable child fork of an immutable parent topology:

- ``reservation_fork`` is the fork's identity and lifecycle, keyed by a bare
  ``reservation_id`` UUID (no cross-schema FK, exactly like
  ``reservation_devices.device_id`` in the reservations service). It holds the
  editable canvas plus a pin to the parent ``TopologyVersion`` it forked from.
- ``fork_connections`` is the lease's real wiring, the unit of the save-time set
  arithmetic. It is a second connection-shaped table so the physical
  ``connections`` graph stays byte-for-byte unchanged.
- ``fork_versions`` mirrors ``TopologyVersion``: an immutable snapshot per save,
  numbered ``max+1`` under a unique constraint.

See docs/design/0001-editable-reservation-topologies.md (Decision 1).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

ForkStatus_ACTIVE = "ACTIVE"
ForkStatus_ARCHIVED = "ARCHIVED"


class ReservationFork(Base):
    __tablename__ = "reservation_fork"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Bare UUID over the service boundary; no FK into the reservations schema.
    # Indexed and unique so re-POSTing an activation is idempotent on the
    # reservation it belongs to.
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True, unique=True
    )
    # Nullable: a reservation may forge wiring with no parent topology
    # (Decision 3 Case A, lazy-create, a later PR).
    parent_topology_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # The parent TopologyVersion this fork was pinned to at activation
    # (Decision 3 Case B). Bare UUID; the parent lives in this same schema but the
    # pin is a snapshot handle, not an enforced FK, so an archived fork survives a
    # parent version delete.
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    canvas_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ForkStatus_ACTIVE, server_default=ForkStatus_ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


_fork_fk = f"{_schema}.reservation_fork.id" if _schema else "reservation_fork.id"
_fork_version_fk = f"{_schema}.fork_versions.id" if _schema else "fork_versions.id"


class ForkConnection(Base):
    __tablename__ = "fork_connections"
    # The unique constraint makes the save-time reconcile a clean set operation and
    # gives the same IntegrityError-on-collision arbiter the VLAN and version code
    # already lean on. Mirrors the physical connections table's per-device indexing.
    __table_args__ = (
        UniqueConstraint(
            "fork_id",
            "device_a_id",
            "port_a",
            "device_b_id",
            "port_b",
            "layer",
            name="uq_fork_connections_endpoints",
        ),
        *(({"schema": _schema},) if _schema else ()),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fork_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_fork_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_a_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    port_a: Mapped[str] = mapped_column(String(255), nullable=False)
    device_b_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    port_b: Mapped[str] = mapped_column(String(255), nullable=False)
    # The "how" the global Connection lacks: L1 (physical) or L2 (VLAN).
    layer: Mapped[str] = mapped_column(String(10), nullable=False)
    # Back-reference to the global connections row a lease wire is realized over
    # for L1; null for pure-L2 intent (open risk #3). Bare UUID, no FK.
    physical_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # The React Flow canvas edge id this hop was resolved from (issue #345 P3b).
    # A single canvas edge can resolve to several switch-touching hops; the execution
    # consumer needs the originating edge to group hops per edge when deriving L1
    # cross-connects, since two edges through the same switch flatten to hops whose
    # correct pairing is otherwise unrecoverable. Deliberately NOT part of the
    # connection identity (ADR 0006 Decision 3). Nullable: rows predating this column,
    # and any hop resolved without a known edge id, carry NULL and consumers must treat
    # NULL as ungrouped.
    edge_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(150), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ForkVersion(Base):
    __tablename__ = "fork_versions"
    # Modeled exactly on TopologyVersion's uq_topology_versions_topology_version:
    # each save appends one immutable snapshot numbered max+1 under this constraint.
    __table_args__ = (
        UniqueConstraint("fork_id", "version_number", name="uq_fork_versions_fork_version"),
        *(({"schema": _schema},) if _schema else ()),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fork_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_fork_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canvas_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    restored_from_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_fork_version_fk, ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
