import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

# Partial-unique index: at most one ACTIVE assignment per (fabric, VLAN). The
# predicate is ACTIVE-only so RELEASED rows do not block VLAN reuse. This is the
# DB-level guard that closes the find_or_assign_vlan check-then-act race; the
# service catches the resulting IntegrityError and retries. Declared here (not
# only in the migration) so Base.metadata.create_all builds it for the SQLite
# unit-test DB. SQLite and Postgres both honor partial unique indexes.
_active_vlan_unique = Index(
    "uq_vlan_active_per_fabric",
    "fabric_id",
    "vlan_id",
    unique=True,
    sqlite_where=text("status = 'ACTIVE'"),
    postgresql_where=text("status = 'ACTIVE'"),
)


class VlanAssignment(Base):
    __tablename__ = "vlan_assignments"
    __table_args__ = (
        (_active_vlan_unique, {"schema": _schema}) if _schema else (_active_vlan_unique,)
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    fabric_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # The allocation's current VLAN-definition scope (issue #442): the transit-inclusive
    # set of L2 switch ids derived from the recorded-hop walk (membership switches PLUS
    # trunk-transit switches). Refreshed on every fork-driven reconcile; the retry-path
    # resolve seeds it with the add switches only, and the next reconcile widens it.
    switch_device_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    # The switches on which create_vlan has CONFIRMED success for this allocation
    # (issue #442, define-on-allocation). Grows on a gated create_vlan success, shrinks
    # on a gated delete_vlan success at last-free; the delete pass targets THIS list,
    # never the scope, so a switch that was never defined is never contacted and a
    # pre-#442 allocation (backfilled empty) drives no unprovable deletes.
    defined_switch_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
