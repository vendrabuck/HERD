"""Create connections table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-28 00:00:00.000000
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
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")

    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("device_a_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("port_a", sa.String(255), nullable=False),
        sa.Column("device_b_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("port_b", sa.String(255), nullable=False),
        sa.Column("connection_type", sa.String(100), nullable=False, server_default="ethernet"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(150), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("modified_by", sa.String(150), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("connections", schema=_schema)
