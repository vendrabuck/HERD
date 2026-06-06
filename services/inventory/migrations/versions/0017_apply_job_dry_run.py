"""Add dry_run column to device_config_apply_jobs.

A dry-run job exercises the driver in simulation mode (record commands,
skip wire I/O) instead of pushing to the device. The schedule endpoint
gates dry-run requests against the driver's `supports_dry_run` flag so an
old driver cannot silently hit the wire on a request that asked for
simulation.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-26 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "device_config_apply_jobs",
        sa.Column(
            "dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_config_apply_jobs", "dry_run", schema=_schema)
