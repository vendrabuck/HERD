"""Create notifications table.

Revision ID: 0001
Revises:
Create Date: 2026-04-20 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.String(length=2048), nullable=False),
        sa.Column(
            "data",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "ix_notifications_user_id",
        "notifications",
        ["user_id"],
        schema=_schema,
    )
    op.create_index(
        "ix_notifications_user_created",
        "notifications",
        ["user_id", "created_at"],
        schema=_schema,
    )
    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["user_id"],
        schema=_schema,
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_unread", table_name="notifications", schema=_schema)
    op.drop_index("ix_notifications_user_created", table_name="notifications", schema=_schema)
    op.drop_index("ix_notifications_user_id", table_name="notifications", schema=_schema)
    op.drop_table("notifications", schema=_schema)
