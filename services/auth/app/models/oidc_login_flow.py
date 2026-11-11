"""The OIDC login flow row (ADR 0016, D2).

One row per sign-in attempt, written by the begin step and consumed by the
callback and the handoff exchange. Every secret the browser or the IdP holds is
stored as its SHA-256 hash only (`state_hash`, `binding_hash`, `handoff_hash`,
through `app.utils.jwt.hash_token`); the two values the callback must send or
compare verbatim are stored raw: `nonce` (compared to the ID token's claim) and
`code_verifier` (sent to the token endpoint, and set to NULL when the callback
claims the row, so a claimed row holds no usable verifier).

The status moves forward only: pending to claimed, then authenticated or
failed, then (authenticated only) exchanged. Every edge is one guarded UPDATE
in `app.services.oidc_flow_service`; the CHECK constraint keeps a value outside
the five out of the column.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""

FLOW_PENDING = "pending"
FLOW_CLAIMED = "claimed"
FLOW_AUTHENTICATED = "authenticated"
FLOW_EXCHANGED = "exchanged"
FLOW_FAILED = "failed"
FLOW_STATUSES = (FLOW_PENDING, FLOW_CLAIMED, FLOW_AUTHENTICATED, FLOW_EXCHANGED, FLOW_FAILED)

_STATUS_CHECK = "status IN ({})".format(", ".join(f"'{s}'" for s in FLOW_STATUSES))


class OidcLoginFlow(Base):
    __tablename__ = "oidc_login_flows"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_oidc_login_flows_state_hash"),
        UniqueConstraint("handoff_hash", name="uq_oidc_login_flows_handoff_hash"),
        CheckConstraint(_STATUS_CHECK, name="ck_oidc_login_flows_status"),
        Index("ix_oidc_login_flows_expires_at", "expires_at"),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(Text, nullable=False)
    binding_hash: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            f"{_fk_prefix}users.id",
            ondelete="CASCADE",
            name="fk_oidc_login_flows_user_id_users",
        ),
        nullable=True,
    )
    handoff_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    handoff_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
