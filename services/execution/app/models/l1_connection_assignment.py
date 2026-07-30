import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None

# Partial-unique index: at most one ACTIVE cross-connect per switch port pair.
# The predicate is ACTIVE-only so RELEASED/FAILED rows never block a new claim
# on the same pair, exactly as uq_vlan_active_per_fabric enforces one VLAN per
# fabric. This is the DB-level guard for the release-before-build reconcile
# (ADR 0007 Decision 4): a moved cable frees its old pair before the new claim,
# and a concurrent connect racing the same pair trips IntegrityError so the
# service retries. Declared here (not only in the migration) so
# Base.metadata.create_all builds it for the SQLite unit-test DB. SQLite and
# Postgres both honor partial unique indexes.
_active_l1_unique = Index(
    "uq_l1_active_per_switch_pair",
    "switch_device_id",
    "port_a",
    "port_b",
    unique=True,
    sqlite_where=text("status = 'ACTIVE'"),
    postgresql_where=text("status = 'ACTIVE'"),
)

# Partial index for the wiring auto-retry sweep's per-tick candidate query
# (due_failed_rows: WHERE status = 'FAILED' AND attempts < max_attempts ORDER BY
# created_at). The table is append-only (ADR 0007 Decision 4: RELEASED/FAILED
# rows are retained as the applied-wiring audit trail), so it grows without
# bound while the sweep runs every tick. Partial-on-FAILED keeps the index
# small and matches the hot predicate directly; attempts is left out of the
# index since it is rarely selective enough on its own append-heavy table to
# be worth the extra column, and the FAILED-only predicate already discards
# the bulk of rows. Declared here (not only in the migration) so
# Base.metadata.create_all builds it for the SQLite unit-test DB.
_failed_l1_created_at = Index(
    "ix_l1_connection_assignments_failed_created_at",
    "created_at",
    sqlite_where=text("status = 'FAILED'"),
    postgresql_where=text("status = 'FAILED'"),
)


class L1ConnectionAssignment(Base):
    """The applied L1 cross-connect state for one switch port pair.

    This table is the connection-addressable applied state (ADR 0007 Decision 4)
    that replaced the execution_runs inference of the retired
    execution_service.action_succeeded_for_reservation helper (deleted in ADR
    0009 phase 7): "is this pair's cross-connect live" is answered by an ACTIVE
    row here, not by scanning execution_runs (which remains the per-action audit
    log). `port_a`/`port_b`
    are the cross-connected switch port pair, canonicalized (sorted) before
    insert so (a, b) and (b, a) collide on the active-unique index.
    `physical_connection_id` is the inventory connection backing the hop; it is
    nullable because phase 1 write sites (and the migration backfill) do not
    carry it, and phase 3's event delta supplies it.

    `intended` (ADR 0009 Decision 2, issue #369) is the direction the row's last
    write was attempting: ACTIVE for a connect/build, RELEASED for a
    disconnect/release. It is tracked separately from `status` so a FAILED row
    records not just "this failed" but "which way it failed", letting the retry
    channels reattempt a failed disconnect as a disconnect rather than defaulting
    every FAILED row to a reconnect. Migration 0016 backfills it for rows that
    predate the column.
    """

    __tablename__ = "l1_connection_assignments"
    __table_args__ = (
        (_active_l1_unique, _failed_l1_created_at, {"schema": _schema})
        if _schema
        else (_active_l1_unique, _failed_l1_created_at)
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    switch_device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    port_a: Mapped[str] = mapped_column(String(128), nullable=False)
    port_b: Mapped[str] = mapped_column(String(128), nullable=False)
    physical_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    intended: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
