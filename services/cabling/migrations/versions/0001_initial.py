"""Initial topologies table.

Revision ID: 0001
Revises:
Create Date: 2026-02-26 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")

    op.create_table(
        "topologies",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("canvas_data", sa.JSON, nullable=True),
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
    op.drop_table("topologies", schema=_schema)
