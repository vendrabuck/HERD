"""Mapping from a directory group DN to a HERD user group (ADR 0011 phase 2).

The DN is the mapping key and is not a stable directory identity: a rename or
OU move leaves the mapping dangling, which the reconciler treats fail-closed
(zero changes for that group) until an admin re-creates it. directory_name is
a display cache; the phase 3 reconciler MUST refresh it on each successful
group fetch (nothing refreshes it yet) and keep the last value on a failed
fetch. herd_group_id is unique: one directory group per HERD group, because
the reconciler's per-mapping set arithmetic cannot converge when two mappings
with different memberships fight over one group. Deleting the HERD group
deletes the mapping via the FK cascade; the directory is never written.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""


class LdapGroupMapping(Base):
    __tablename__ = "ldap_group_mappings"
    # Constraint names match migration 0006 so create_all and migration-managed
    # schemas do not drift.
    __table_args__ = (
        UniqueConstraint("group_dn", name="uq_ldap_group_mappings_group_dn"),
        UniqueConstraint("herd_group_id", name="uq_ldap_group_mappings_herd_group_id"),
        *(({"schema": _schema},) if _schema else ()),
    )
    # Fold server defaults (created_at) into INSERT ... RETURNING so no
    # post-commit refresh round-trip is needed.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_dn: Mapped[str] = mapped_column(Text, nullable=False)
    directory_name: Mapped[str] = mapped_column(String(255), nullable=False)
    herd_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}user_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
