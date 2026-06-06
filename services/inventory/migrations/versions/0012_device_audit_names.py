"""Add created_by, created_by_name, modified_by_name to devices.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-17 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column("created_by_name", sa.String(255), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column("modified_by_name", sa.String(255), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("devices", "modified_by_name", schema=_schema)
    op.drop_column("devices", "created_by_name", schema=_schema)
    op.drop_column("devices", "created_by", schema=_schema)
