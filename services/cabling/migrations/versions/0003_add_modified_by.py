"""Add modified_by to topologies.

Revision ID: 0003
Revises: 0002
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
        "topologies",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("topologies", "modified_by", schema=_schema)
