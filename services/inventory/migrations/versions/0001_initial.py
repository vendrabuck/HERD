"""Initial inventory table: devices.

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
        "devices",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "device_type",
            sa.Enum(
                "FIREWALL", "SWITCH", "ROUTER", "TRAFFIC_SHAPER", "OTHER",
                name="devicetype", schema=_schema,
            ),
            nullable=False,
        ),
        sa.Column(
            "topology_type",
            sa.Enum("PHYSICAL", "CLOUD", name="topologytype", schema=_schema),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "AVAILABLE", "RESERVED", "OFFLINE", "MAINTENANCE",
                name="devicestatus", schema=_schema,
            ),
            nullable=False,
            server_default="AVAILABLE",
        ),
        sa.Column("location", sa.String(500), nullable=True),
        sa.Column("specs", sa.JSON, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
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
    op.drop_table("devices", schema=_schema)
