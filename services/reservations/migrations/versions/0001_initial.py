"""Initial reservations table.

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000
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
        "reservations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("device_ids", sa.JSON, nullable=False),
        sa.Column(
            "topology_type",
            sa.Enum("PHYSICAL", "CLOUD", name="topologytype", schema=_schema),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text, nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "ACTIVE", "COMPLETED", "CANCELLED",
                name="reservationstatus", schema=_schema,
            ),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("reservations", schema=_schema)
