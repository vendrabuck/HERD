"""Add connection_type column to driver_packages.

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-16 12:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "driver_packages",
        sa.Column(
            "connection_type",
            sa.String(50),
            nullable=False,
            server_default="Management",
        ),
        schema=_schema,
    )
    # Remove server_default so new rows require an explicit value
    with op.batch_alter_table("driver_packages", schema=_schema) as batch_op:
        batch_op.alter_column("connection_type", server_default=None)


def downgrade() -> None:
    op.drop_column("driver_packages", "connection_type", schema=_schema)
