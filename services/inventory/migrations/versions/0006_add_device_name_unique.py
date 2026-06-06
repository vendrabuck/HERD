"""Add unique constraint on devices.name.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-07 00:00:00.000000
"""

import os

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_schema = os.getenv("DB_SCHEMA", "inventory")


def upgrade() -> None:
    op.create_unique_constraint("uq_devices_name", "devices", ["name"], schema=_schema)


def downgrade() -> None:
    op.drop_constraint("uq_devices_name", "devices", schema=_schema)
