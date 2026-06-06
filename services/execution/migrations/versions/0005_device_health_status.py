"""Create device_health_status table for periodic polling state.

One row per polled device, storing the current snapshot:
last_polled_at, last_status (UNKNOWN/HEALTHY/DEGRADED/UNREACHABLE),
last_run_id (link to the execution_runs row), consecutive_failures, and
next_poll_at. The scheduler scans WHERE next_poll_at <= now() every tick;
the index on next_poll_at is the hot path.

Per-poll history persists in execution_runs (every poll writes three rows:
login, status, logout). This table only tracks the current snapshot and the
scheduler's bookkeeping.

device_id is a plain UUID with no FK: cross-schema FKs to inventory.devices
are forbidden per HERD's per-service-schema rule.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-26 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_runs_fk = f"{_schema}.execution_runs.id" if _schema else "execution_runs.id"


def upgrade() -> None:
    op.create_table(
        "device_health_status",
        sa.Column("device_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_status",
            sa.String(20),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column(
            "last_run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_runs_fk, ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "next_poll_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "ix_device_health_status_next_poll_at",
        "device_health_status",
        ["next_poll_at"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_device_health_status_next_poll_at",
        table_name="device_health_status",
        schema=_schema,
    )
    op.drop_table("device_health_status", schema=_schema)
