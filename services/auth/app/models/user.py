import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None
_fk_prefix = f"{settings.db_schema}." if settings.db_schema else ""


class Role(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local", server_default="local"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # ADR 0011 phase 4: True only when the LDAP deactivation sweep is the one
    # that flipped is_active False. Gates reactivation eligibility: an
    # admin-deactivated user (this stays False) is never touched by the
    # sweep, since admin intent always outranks the directory. The manual
    # activate/deactivate endpoints always write this False.
    deactivated_by_sync: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    role: Mapped[Role] = mapped_column(
        Enum(Role, schema=_schema), nullable=False, default=Role.USER
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(f"{_fk_prefix}users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")
