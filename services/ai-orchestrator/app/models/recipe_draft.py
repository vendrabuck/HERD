"""AI-drafted recipe packages awaiting admin review (ADR 0005, issue #28).

One row per drafting session. Drafts are admin-scoped working artifacts, not
reservation conversations, so they deliberately do not reuse the assistant
conversation tables. The stored files plus the validation report are the
review surface; the package archive is assembled on demand from the stored
files, never stored as a blob. Rows never auto-promote to anything: upload
remains the admin's explicit action through inventory's existing endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class RecipeDraft(Base):
    __tablename__ = "recipe_drafts"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    hypervisor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    driver_py: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded driver_metadata.json content (provenance already injected).
    driver_metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded validation report from execution's validate-package.
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
