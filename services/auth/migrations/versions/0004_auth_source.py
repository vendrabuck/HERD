"""Add auth_source column and make hashed_password nullable on users.

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
        "users",
        sa.Column(
            "auth_source",
            sa.String(length=16),
            nullable=False,
            server_default="local",
        ),
        schema=_schema,
    )
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.Text(),
        nullable=True,
        schema=_schema,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.Text(),
        nullable=False,
        schema=_schema,
    )
    op.drop_column("users", "auth_source", schema=_schema)
