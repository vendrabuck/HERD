"""Add poll_tier to device_health_status for event-driven tiered polling.

Issue #24: a device under an active reservation polls on the in-use cadence
and returns to idle when the reservation ends, driven by the consumed
reservation lifecycle events. The tier is persisted on the status row (not
derived at poll time) because the events are acked exactly once and never
replay, so an in-memory tier would be lost on a service restart.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-08 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "device_health_status",
        sa.Column("poll_tier", sa.String(10), nullable=False, server_default="idle"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_health_status", "poll_tier", schema=_schema)
