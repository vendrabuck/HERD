"""Add driver_type to device_templates and create driver_packages table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-06 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None

_devices_table = f"{_schema}.devices" if _schema else "devices"


def upgrade() -> None:
    # Add driver_type column to device_templates
    op.add_column(
        "device_templates",
        sa.Column("driver_type", sa.String(50), nullable=True),
        schema=_schema,
    )

    # Create driver_packages table
    op.create_table(
        "driver_packages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_devices_table}.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_by", sa.String(255), nullable=False),
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
    op.drop_table("driver_packages", schema=_schema)
    op.drop_column("device_templates", "driver_type", schema=_schema)
