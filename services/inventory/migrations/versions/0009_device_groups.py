"""Add device groups, device group devices, and device group permissions tables.

Revision ID: 0009
Revises: 0008
Create Date: 2026-03-15 12:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    devices_ref = f"{_schema}.devices" if _schema else "devices"

    op.create_table(
        "device_groups",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        schema=_schema,
    )

    device_groups_ref = f"{_schema}.device_groups" if _schema else "device_groups"

    op.create_table(
        "device_group_devices",
        sa.Column(
            "device_group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{device_groups_ref}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "device_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{devices_ref}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("device_group_id", "device_id"),
        schema=_schema,
    )

    op.create_table(
        "device_group_permissions",
        sa.Column(
            "device_group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{device_groups_ref}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_group_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("assigned_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("device_group_id", "user_group_id"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("device_group_permissions", schema=_schema)
    op.drop_table("device_group_devices", schema=_schema)
    op.drop_table("device_groups", schema=_schema)
