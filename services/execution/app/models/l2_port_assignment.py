import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

# Partial-unique index: at most one ACTIVE membership per (switch, port, VLAN
# allocation). Mirrors uq_l1_active_per_switch_pair and uq_vlan_active_per_fabric:
# RELEASED/FAILED rows are retained and never block a new claim on the same
# (switch, port, vlan_assignment_id).
_active_l2_unique = Index(
    "uq_l2_active_per_switch_port_vlan",
    "switch_device_id",
    "port",
    "vlan_assignment_id",
    unique=True,
    sqlite_where=text("status = 'ACTIVE'"),
    postgresql_where=text("status = 'ACTIVE'"),
)

# Partial index for the future retry sweep's per-tick candidate query, mirroring
# ix_l1_connection_assignments_failed_created_at (issue #390 pattern): FAILED-only,
# ordered by created_at, so the sweep is an index scan rather than a sequential scan
# plus sort on an append-heavy table.
_failed_l2_created_at = Index(
    "ix_l2_port_assignments_failed_created_at",
    "created_at",
    sqlite_where=text("status = 'FAILED'"),
    postgresql_where=text("status = 'FAILED'"),
)


class L2PortAssignment(Base):
    """Per-port L2 VLAN membership ledger (ADR 0009 Decision 2, issues #369/#416).

    Schema only in this phase: nothing in the service reads or writes this table
    yet. It is introduced now so the layered L2 reconcile (ADR 0009 phase 4,
    docs/design/0009-l2-l3-connection-driven-reconcile.md) has its ledger ready
    when that phase lands.

    Splits allocation from membership. `vlan_assignments` stays the per-fabric
    VLAN-number allocation (find-or-assign, written before any driver call since
    allocation is a database decision, not a hardware outcome). This table
    records the DRIVER-CONFIRMED per-port membership in that allocation: one row
    per (switch, port) the reconcile put into or took out of the fabric.
    `status` reflects the driver outcome only and is never written ACTIVE before
    hardware confirms, closing the gap where `vlan_assignments` alone cannot
    distinguish "the VLAN number is allocated" from "this specific port is a
    member of it".

    `intended` mirrors `l1_connection_assignments.intended`: the direction
    (ACTIVE for join, RELEASED for leave) the row's last write was attempting,
    so the phase 4/5 retry channels can honor it exactly as the L1 retry channel
    does for cross-connects (issue #369).
    """

    __tablename__ = "l2_port_assignments"
    __table_args__ = (
        (_active_l2_unique, _failed_l2_created_at, {"schema": _schema})
        if _schema
        else (_active_l2_unique, _failed_l2_created_at)
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    vlan_assignment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    switch_device_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    port: Mapped[str] = mapped_column(String(128), nullable=False)
    intended: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
