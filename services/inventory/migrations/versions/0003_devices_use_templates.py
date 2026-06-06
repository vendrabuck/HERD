"""Migrate devices from DeviceType enum to template_id FK.

Drops: device_type, location, specs, description columns.
Adds: template_id (FK to device_templates), field_data (JSON).

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-03 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None

_templates_table = f"{_schema}.device_templates" if _schema else "device_templates"


def upgrade() -> None:
    # Add new columns
    op.add_column(
        "devices",
        sa.Column(
            "template_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_templates_table}.id"),
            nullable=True,
        ),
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column("field_data", sa.JSON, nullable=False, server_default="{}"),
        schema=_schema,
    )

    # Drop old columns
    op.drop_column("devices", "device_type", schema=_schema)
    op.drop_column("devices", "location", schema=_schema)
    op.drop_column("devices", "specs", schema=_schema)
    op.drop_column("devices", "description", schema=_schema)


def downgrade() -> None:
    # Re-add old columns
    op.add_column(
        "devices",
        sa.Column("description", sa.Text, nullable=True),
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column("specs", sa.JSON, nullable=True),
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column("location", sa.String(500), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column(
            "device_type",
            sa.Enum(
                "FIREWALL",
                "SWITCH",
                "ROUTER",
                "TRAFFIC_SHAPER",
                "OTHER",
                name="devicetype",
                schema=_schema,
            ),
            nullable=True,
        ),
        schema=_schema,
    )

    # Drop new columns
    op.drop_column("devices", "field_data", schema=_schema)
    op.drop_column("devices", "template_id", schema=_schema)
