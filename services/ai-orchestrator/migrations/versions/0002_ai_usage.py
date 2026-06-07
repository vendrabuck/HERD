"""ai_usage table for per-user daily AI token quotas.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-07 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("usage_date", sa.Date, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "uq_ai_usage_user_date",
        "ai_usage",
        ["user_id", "usage_date"],
        unique=True,
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("uq_ai_usage_user_date", table_name="ai_usage", schema=_schema)
    op.drop_table("ai_usage", schema=_schema)
