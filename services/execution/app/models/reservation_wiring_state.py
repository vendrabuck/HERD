import uuid

from sqlalchemy import Boolean, Integer, Uuid, false
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class ReservationWiringState(Base):
    """Per-reservation ordering and teardown anchor for connection-driven apply.

    Companion to l1_connection_assignments (ADR 0007 Decision 4). One row per
    reservation. `last_applied_fork_version` is the monotonic marker the phase 3
    consumer uses to skip stale/duplicate deliveries and to detect a version gap;
    phase 1 creates the table but never stamps it (nullable, left for phase 3 and
    the backfill baseline). `frozen` is set true by the terminal-event handlers
    (reservation.cancelled/completed/failed) so that a wiring_changed event
    arriving after teardown is a safe no-op (Decision 7); phase 1 sets it as an
    inert write and nothing reads it yet.
    """

    __tablename__ = "reservation_wiring_state"
    __table_args__ = ({"schema": _schema},) if _schema else ()

    reservation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    last_applied_fork_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frozen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
