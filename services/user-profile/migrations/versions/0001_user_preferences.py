"""Create user_preferences table.

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
        "user_preferences",
        sa.Column("user_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "saved_filters",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "page_sizes",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "extras",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("user_preferences", schema=_schema)
