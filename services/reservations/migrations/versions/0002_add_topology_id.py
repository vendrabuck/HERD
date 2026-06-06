"""Add topology_id to reservations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-12 00:00:00.000000
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
    op.add_column(
        "reservations",
        sa.Column("topology_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_reservations_topology_id",
        "reservations",
        ["topology_id"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("ix_reservations_topology_id", table_name="reservations", schema=_schema)
    op.drop_column("reservations", "topology_id", schema=_schema)
