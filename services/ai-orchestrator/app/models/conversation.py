"""Multi-turn assistant conversation storage.

A conversation is scoped to (user_id, reservation_id). The seed_block is
rendered once at conversation creation and persisted as the first user
message; subsequent turns append messages. Eviction (turn cap + token
budget) and TTL sweeping happen at the repository layer; the models here
are intentionally schemaless on content (Anthropic-style content blocks
are persisted as JSON so the route can replay them verbatim into the
provider call).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.config import settings
from app.database import Base

# Resolve schema once at import. Empty string (test env: sqlite + DB_SCHEMA="")
# becomes None so the tables land in the default namespace; Postgres uses the
# configured schema. Matches the pattern in services/reservations/app/models/.
_schema = settings.db_schema or None


class MessageRole(str, enum.Enum):
    """Role on a stored AssistantMessage.

    `user`, `assistant`, and `tool` map to the Anthropic Message roles used in
    the provider call. `tool` is a user-role message in Anthropic's API but
    carries only tool_result blocks; we tag it separately so the repo can
    distinguish a real user turn (which counts against the turn cap) from a
    tool-result echo (which does not).
    """

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"


# Per-service convention: use JSONB on Postgres for queryability, JSON on
# SQLite (tests) for compatibility. Same column declaration both ways via
# with_variant; we pick JSONB at write time and SQLAlchemy maps it down for
# sqlite tests transparently.
_JsonType = JSON().with_variant(JSONB(), "postgresql")


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        Index(
            "ix_assistant_conversations_user_reservation",
            "user_id",
            "reservation_id",
        ),
        # Serves the purpose-classification transcript read, which filters on
        # reservation_id alone and orders by created_at (issue #712, migration
        # 0005). The (user_id, reservation_id) composite above cannot serve a
        # reservation_id-only predicate; it is kept for now, drop is a follow-up.
        Index(
            "ix_assistant_conversations_reservation_created",
            "reservation_id",
            "created_at",
        ),
        Index("ix_assistant_conversations_last_used_at", "last_used_at"),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reservation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    seed_block: Mapped[str] = mapped_column(Text, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    messages: Mapped[list[AssistantMessage]] = relationship(
        "AssistantMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.position",
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        Index(
            "ix_assistant_messages_conversation_position",
            "conversation_id",
            "position",
        ),
        {"schema": _schema} if _schema else {},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            f"{_schema}.assistant_conversations.id" if _schema else "assistant_conversations.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="assistantmessagerole", schema=_schema),
        nullable=False,
    )
    # Anthropic-style content blocks (list of TextBlock | ToolUseBlock |
    # ToolResultBlock dicts), persisted verbatim so the route can rehydrate
    # them into Message objects for the provider call without translation.
    content_blocks: Mapped[list] = mapped_column(_JsonType, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[AssistantConversation] = relationship(
        "AssistantConversation", back_populates="messages"
    )
