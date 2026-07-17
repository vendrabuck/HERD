"""Fork wiring ledger: the anchor for reservation.wiring_changed staging (ADR 0007).

One row per reservation records the fork_version of the last wiring_changed event this
service staged. It is upserted in the SAME transaction as the outbox enqueue (see
`stage_wiring_changed` in reservation_service), so the event exists if and only if the
ledger advanced: both are one commit. That is what closes the save-then-stage crash
gap (ADR 0007 Decision 2): a save that committed a new fork_version cabling-side but
whose event never staged leaves the ledger behind cabling's latest, and the standing
sweeper (`_run_fork_archive_reconcile`) heals it by staging a delta-less heal event and
advancing the ledger.

Schema-per-service: the table lands in the reservations schema via __table_args__, the
same idiom the outbox and reservation tables use. No cross-schema FK: reservation_id is
a bare UUID keyed to reservations' own rows.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class ForkWiringLedger(Base):
    __tablename__ = "fork_wiring_ledger"
    __table_args__ = {"schema": _schema} if _schema else {}

    reservation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    last_staged_fork_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
