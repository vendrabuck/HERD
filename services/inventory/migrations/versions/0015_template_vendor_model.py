"""Add vendor, model, part_number to device_templates.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-21 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_table = f"{_schema}.device_templates" if _schema else "device_templates"


def upgrade() -> None:
    op.add_column(
        "device_templates",
        sa.Column("vendor", sa.String(255), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "device_templates",
        sa.Column("model", sa.String(255), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "device_templates",
        sa.Column("part_number", sa.String(255), nullable=True),
        schema=_schema,
    )
    op.execute(f"UPDATE {_table} SET vendor = 'unknown' WHERE vendor IS NULL")
    op.execute(f"UPDATE {_table} SET model = 'unknown' WHERE model IS NULL")
    op.alter_column(
        "device_templates",
        "vendor",
        nullable=False,
        schema=_schema,
    )
    op.alter_column(
        "device_templates",
        "model",
        nullable=False,
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_templates", "part_number", schema=_schema)
    op.drop_column("device_templates", "model", schema=_schema)
    op.drop_column("device_templates", "vendor", schema=_schema)
