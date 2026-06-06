"""Add temporal and status indexes on reservations to speed the utilization report.

Revision ID: 0007
Revises: 0006

The utilization report filters on `end_time > window_start AND start_time < window_end`
with an optional `status IN (...)`. Without these indexes the scan grows linearly with
total reservation count; with them, Postgres can range-scan just the affected window.

Postgres-only: SQLite (unit tests) builds tables in-memory per test and does not benefit.
"""

import os

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_reservations_start_time "
        f"ON {_qualified('reservations')} (start_time)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_reservations_end_time "
        f"ON {_qualified('reservations')} (end_time)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_reservations_status "
        f"ON {_qualified('reservations')} (status)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"DROP INDEX IF EXISTS {_qualified('ix_reservations_status')}")
    op.execute(f"DROP INDEX IF EXISTS {_qualified('ix_reservations_end_time')}")
    op.execute(f"DROP INDEX IF EXISTS {_qualified('ix_reservations_start_time')}")
