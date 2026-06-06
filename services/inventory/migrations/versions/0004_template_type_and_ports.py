"""Add template_type to device_templates and create ports table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-05 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None

_devices_table = f"{_schema}.devices" if _schema else "devices"
_templates_table = f"{_schema}.device_templates" if _schema else "device_templates"


def upgrade() -> None:
    # Add template_type column to device_templates
    op.add_column(
        "device_templates",
        sa.Column(
            "template_type",
            sa.String(50),
            nullable=False,
            server_default="device",
        ),
        schema=_schema,
    )

    # Create ports table
    op.create_table(
        "ports",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "device_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_devices_table}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "template_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_templates_table}.id"),
            nullable=False,
        ),
        sa.Column("field_data", sa.JSON, nullable=False, server_default="{}"),
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
    op.drop_table("ports", schema=_schema)
    op.drop_column("device_templates", "template_type", schema=_schema)
