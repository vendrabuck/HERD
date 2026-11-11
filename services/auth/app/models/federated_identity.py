"""A federated identity: one IdP account linked to one HERD account (ADR 0016, D4).

The key is the pair (`issuer`, `subject`) from a verified ID token, never the
email. A row is created only together with its `auth_source="oidc"` user, in one
transaction (`auth_service.create_oidc_user`), and each user has at most one
row. A separate table rather than columns on `users`, so a later SAML identity
can be keyed the same way without another `users` migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""

# The width of `subject`; a longer `sub` is refused at provisioning rather than
# left to the database to reject.
SUBJECT_MAX_LENGTH = 255


class FederatedIdentity(Base):
    __tablename__ = "federated_identities"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_federated_identities_user_id"),
        UniqueConstraint("issuer", "subject", name="uq_federated_identities_issuer_subject"),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            f"{_fk_prefix}users.id",
            ondelete="CASCADE",
            name="fk_federated_identities_user_id_users",
        ),
        nullable=False,
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(SUBJECT_MAX_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
