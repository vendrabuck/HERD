"""Add modified_by to devices, device_templates, driver_packages, device_groups;
add updated_at to device_groups.

Revision ID: 0011
Revises: 0010
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "device_templates",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "driver_packages",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "device_groups",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.add_column(
        "device_groups",
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_groups", "modified_by", schema=_schema)
    op.drop_column("device_groups", "updated_at", schema=_schema)
    op.drop_column("driver_packages", "modified_by", schema=_schema)
    op.drop_column("device_templates", "modified_by", schema=_schema)
    op.drop_column("devices", "modified_by", schema=_schema)
