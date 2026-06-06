"""Add exclusive column to device_templates.

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-09 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "device_templates",
        sa.Column("exclusive", sa.Boolean, nullable=False, server_default="1"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_templates", "exclusive", schema=_schema)
