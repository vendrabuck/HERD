"""Add device_templates table.

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-02 00:00:00.000000
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
    op.create_table(
        "device_templates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("icon", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("sections", sa.JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("device_templates", schema=_schema)
