"""Relink candidates: the identities of refused single sign-on logins (issue #1399).

Owner decision of 2026-11-18 (ADR 0016 D4 as built). When a first login is
refused because its email belongs to an existing `oidc` account (IAM-OIDC-10),
the refused (issuer, subject) is kept here against that account, so a
superadmin can relink the account to it without reading the subject at the
provider (Microsoft Entra ID never shows it). A candidate is evidence that
SOMEONE signed in at the provider with an account carrying that email, never
proof that it is the person: nothing links from this table on its own, and the
superadmin confirms out of band before a relink names a candidate.

`federated_relink_candidates` holds at most `CANDIDATES_PER_ACCOUNT` live rows
per account and `CANDIDATES_GLOBAL_CAP` in all, each living `CANDIDATE_TTL`
from its first capture; a repeat of the same pair bumps `attempts` and
`last_seen_at` only. `federated_relink_capture_totals` counts, per account and
forever, the distinct pairs ever captured, so eviction by the caps is visible
(`captured_total` grows while the live list cannot).

Neither table is ever logged from. The candidate LIST route never returns the
subject (it shows the pair's fingerprint); the relink by `candidate_id` reads it
and, like every relink, answers the identity it linked, subject included
(IAM-OIDC-31). Expired rows are deleted only when a later capture runs.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base
from app.models.federated_identity import SUBJECT_MAX_LENGTH

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""

# Fixed in code, like the flow store's TTLs (ADR 0016, Settings: "Fixed in
# code, not settings"); the spec states each value (IAM-OIDC-36).
CANDIDATE_TTL = timedelta(hours=24)
CANDIDATES_PER_ACCOUNT = 5
CANDIDATES_GLOBAL_CAP = 1000


class FederatedRelinkCandidate(Base):
    __tablename__ = "federated_relink_candidates"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "issuer", "subject", name="uq_federated_relink_candidates_user_pair"
        ),
        Index("ix_federated_relink_candidates_created_at", "created_at"),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            f"{_fk_prefix}users.id",
            ondelete="CASCADE",
            name="fk_federated_relink_candidates_user_id_users",
        ),
        nullable=False,
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(SUBJECT_MAX_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class FederatedRelinkCaptureTotal(Base):
    __tablename__ = "federated_relink_capture_totals"
    __table_args__ = ({"schema": _schema} if _schema else {},)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            f"{_fk_prefix}users.id",
            ondelete="CASCADE",
            name="fk_federated_relink_capture_totals_user_id_users",
        ),
        primary_key=True,
    )
    captured_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
