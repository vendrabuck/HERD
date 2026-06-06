"""Convert reservations.device_ids to JSONB and add a GIN index.

Revision ID: 0005
Revises: 0004

Postgres-only: SQLite treats JSON and JSONB identically and skips the GIN index.
"""

import os

from alembic import op

revision = "0005"
down_revision = "0004"
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
        f"ALTER TABLE {_qualified('reservations')} "
        f"ALTER COLUMN device_ids TYPE JSONB USING device_ids::jsonb"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_reservations_device_ids "
        f"ON {_qualified('reservations')} USING GIN (device_ids jsonb_path_ops)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"DROP INDEX IF EXISTS {_qualified('ix_reservations_device_ids')}")
    op.execute(
        f"ALTER TABLE {_qualified('reservations')} "
        f"ALTER COLUMN device_ids TYPE JSON USING device_ids::json"
    )
