"""Secret metadata plus encrypted payload, and the wrapped-DEK key ring (issue #39).

The plaintext of a secret's data dict is JSON-serialized and encrypted as one
blob (ADR 0003, decision point 2): metadata columns are plaintext, the payload
is ciphertext + nonce + key_version. key_versions holds each data-encryption
key wrapped by the environment-supplied KEK; see app/services/crypto.py for the
encryption boundary.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class KeyVersion(Base):
    __tablename__ = "key_versions"
    __table_args__ = (*([{"schema": _schema}] if _schema else [{}]),)

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # nonce || ciphertext of the DEK, AES-GCM under the KEK with the version as AAD.
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _fk_target(table: str, column: str) -> str:
    return f"{_schema}.{table}.{column}" if _schema else f"{table}.{column}"


class Secret(Base):
    __tablename__ = "secrets"
    __table_args__ = (*([{"schema": _schema}] if _schema else [{}]),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # api_key | ssh_key | password | token | generic (validated at the schema layer).
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="generic")
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(
        Integer, ForeignKey(_fk_target("key_versions", "version")), nullable=False
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
