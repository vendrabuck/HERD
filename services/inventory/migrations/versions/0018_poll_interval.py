"""Add poll_interval_seconds to devices and device_templates.

Per-device opt-in for periodic health polling. Device value wins; falls
back to template value; if both NULL, the device is not polled. Scheduler
in the execution service resolves the fallback when reading the inventory
health-config endpoint.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-26 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "device_templates",
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_templates", "poll_interval_seconds", schema=_schema)
    op.drop_column("devices", "poll_interval_seconds", schema=_schema)
