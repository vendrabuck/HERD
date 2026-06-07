"""Add prompt-caching token counters to ai_usage.

cache_creation_input_tokens and cache_read_input_tokens accumulate Anthropic
prompt-caching activity for observability. They are recorded alongside the
quota-bearing input/output counts but are NOT summed into the daily-quota
total (usage_repo.get_today_total / enforce_quota meter input + output only),
since cache reads and writes are billed at a fraction of the base rate.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-07 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "ai_usage",
        sa.Column(
            "cache_creation_input_tokens",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        schema=_schema,
    )
    op.add_column(
        "ai_usage",
        sa.Column(
            "cache_read_input_tokens",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("ai_usage", "cache_read_input_tokens", schema=_schema)
    op.drop_column("ai_usage", "cache_creation_input_tokens", schema=_schema)
