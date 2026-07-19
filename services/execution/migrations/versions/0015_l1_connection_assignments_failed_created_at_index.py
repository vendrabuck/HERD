"""Index l1_connection_assignments for the FAILED-row retry sweep.

due_failed_rows (app/services/l1_assignment_service.py) is the per-tick
candidate query for the background wiring auto-retry channel: it filters
`status = 'FAILED' AND attempts < max_attempts`, orders by `created_at`, and
applies a batch LIMIT. The table carries no index covering that predicate, so
Postgres does a sequential scan plus an in-memory sort on every tick,
regardless of whether any row is actually due.

The table is append-only by design (ADR 0007 Decision 4): RELEASED and FAILED
rows are retained as the applied-wiring audit trail and never deleted, so the
scanned set grows without bound as wiring activity accumulates across the
fleet.

Adds ix_l1_connection_assignments_failed_created_at: a partial index on
created_at, restricted to status = 'FAILED', so it stays small and serves the
sweep's ORDER BY as an index scan instead of a full-table sort.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-19 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_index(
        "ix_l1_connection_assignments_failed_created_at",
        "l1_connection_assignments",
        ["created_at"],
        schema=_schema,
        postgresql_where=sa.text("status = 'FAILED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_l1_connection_assignments_failed_created_at",
        table_name="l1_connection_assignments",
        schema=_schema,
    )
