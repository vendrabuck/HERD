"""Add metadata_json column to driver_cache for driver_metadata.json capture.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "driver_cache",
        sa.Column("metadata_json", sa.Text(), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("driver_cache", "metadata_json", schema=_schema)
