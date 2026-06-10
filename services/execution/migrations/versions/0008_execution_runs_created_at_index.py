"""Index execution_runs.created_at for the desc-ordered listing and windowed count.

list_runs always sorts by created_at DESC and the reporting service queries a
created_at window with a COUNT (fetch_execution_run_count). The table grows
without bound: the health-poll scheduler writes three rows per device per poll,
plus a row per provisioning step. With no index, the default unfiltered admin
listing did a full scan plus an in-memory sort, and the windowed count a full
scan, both O(R) to O(R log R) in total rows.

Adds:
- ix_execution_runs_created_at: serves the default `ORDER BY created_at DESC`
  listing as an index scan.
- ix_execution_runs_device_created_at, ix_execution_runs_reservation_created_at:
  composite (equality, created_at DESC) indexes that serve the filtered
  listings and the reporting count in a single index, where the standalone
  created_at index cannot. The existing standalone device_id / reservation_id
  indexes are left in place.

The DESC qualifier is a no-op on SQLite (unit tests) and used by Postgres.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-10 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_index(
        "ix_execution_runs_created_at",
        "execution_runs",
        [sa.text("created_at DESC")],
        schema=_schema,
    )
    op.create_index(
        "ix_execution_runs_device_created_at",
        "execution_runs",
        ["device_id", sa.text("created_at DESC")],
        schema=_schema,
    )
    op.create_index(
        "ix_execution_runs_reservation_created_at",
        "execution_runs",
        ["reservation_id", sa.text("created_at DESC")],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_runs_reservation_created_at",
        table_name="execution_runs",
        schema=_schema,
    )
    op.drop_index(
        "ix_execution_runs_device_created_at",
        table_name="execution_runs",
        schema=_schema,
    )
    op.drop_index(
        "ix_execution_runs_created_at",
        table_name="execution_runs",
        schema=_schema,
    )
