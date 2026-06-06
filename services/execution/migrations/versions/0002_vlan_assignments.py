"""Create vlan_assignments table for fabric-aware VLAN tracking.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-16 00:00:00.000000
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
        "vlan_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("fabric_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("switch_device_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, default="ACTIVE"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )

    # Index for conflict queries: find active VLANs in a fabric
    op.create_index(
        "ix_vlan_assignments_fabric_status",
        "vlan_assignments",
        ["fabric_id", "status"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_vlan_assignments_fabric_status",
        table_name="vlan_assignments",
        schema=_schema,
    )
    op.drop_table("vlan_assignments", schema=_schema)
