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
    switch_device_ids: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
