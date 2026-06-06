"""Initial ai-orchestrator schema: assistant_conversations + assistant_messages.

Revision ID: 0001
Revises:
Create Date: 2026-05-29 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")

    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("seed_block", sa.Text, nullable=False),
        sa.Column("turn_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "ix_assistant_conversations_user_reservation",
        "assistant_conversations",
        ["user_id", "reservation_id"],
        schema=_schema,
    )
    op.create_index(
        "ix_assistant_conversations_last_used_at",
        "assistant_conversations",
        ["last_used_at"],
        schema=_schema,
    )

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                f"{_schema}.assistant_conversations.id"
                if _schema
                else "assistant_conversations.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum("USER", "ASSISTANT", "TOOL", name="assistantmessagerole", schema=_schema),
            nullable=False,
        ),
        sa.Column(
            "content_blocks",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "ix_assistant_messages_conversation_position",
        "assistant_messages",
        ["conversation_id", "position"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_messages_conversation_position",
        table_name="assistant_messages",
        schema=_schema,
    )
    op.drop_table("assistant_messages", schema=_schema)
    op.drop_index(
        "ix_assistant_conversations_last_used_at",
        table_name="assistant_conversations",
        schema=_schema,
    )
    op.drop_index(
        "ix_assistant_conversations_user_reservation",
        table_name="assistant_conversations",
        schema=_schema,
    )
    op.drop_table("assistant_conversations", schema=_schema)
    if _schema:
        op.execute(f"DROP TYPE IF EXISTS {_schema}.assistantmessagerole")
