"""Add updated_at and modified_by to reservations.

Revision ID: 0004
Revises: 0003
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
        "reservations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservations", "modified_by", schema=_schema)
    op.drop_column("reservations", "updated_at", schema=_schema)
