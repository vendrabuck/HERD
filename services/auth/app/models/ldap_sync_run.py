"""Audit row for one directory group sync pass (ADR 0011 phase 3).

A run row is committed with status "running" before any directory work
starts, so a crashed pass leaves a visible corpse instead of silence.
Counters and detail carry BOTH a client-side default (default=) and a
matching server_default (C8): the client side covers the ORM-constructed
row this service always inserts, and the server_default keeps a fresh
create_all schema in parity with migration 0007's raw DDL, so a row
inserted by provenance other than this model (a raw-SQL insert, or any
future writer) still lands at 0/{} instead of NULL. detail is a capped,
categorized record of skips and failures; the cap constant lives with the
reconciler (ldap_sync_service.py), not here. users_deactivated and
users_reactivated (ADR 0011 phase 4) follow the same C8 parity pattern;
the "aborted" status they can drive is a plain string in the existing
status column, no schema change needed for it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class LdapSyncRun(Base):
    __tablename__ = "ldap_sync_runs"
    __table_args__ = {"schema": _schema} if _schema else {}
    # Fold server defaults (started_at, the counters, detail) into
    # INSERT ... RETURNING so no post-commit refresh round-trip is needed.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    users_provisioned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    members_added: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    members_removed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    members_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    users_deactivated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    users_reactivated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    detail: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
