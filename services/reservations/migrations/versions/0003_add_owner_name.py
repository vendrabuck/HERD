"""Add owner_name to reservations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-12 00:00:00.000000
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
        "reservations",
        sa.Column("owner_name", sa.String(150), nullable=True, server_default=""),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservations", "owner_name", schema=_schema)
