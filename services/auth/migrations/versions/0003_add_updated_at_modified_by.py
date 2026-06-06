"""Add updated_at and modified_by to users and user_groups.

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
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.add_column(
        "users",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "user_groups",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.add_column(
        "user_groups",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("user_groups", "modified_by", schema=_schema)
    op.drop_column("user_groups", "updated_at", schema=_schema)
    op.drop_column("users", "modified_by", schema=_schema)
    op.drop_column("users", "updated_at", schema=_schema)
