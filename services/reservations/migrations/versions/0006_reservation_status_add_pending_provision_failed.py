"""Add PENDING_PROVISION and FAILED to reservationstatus enum.

Revision ID: 0006
Revises: 0005

Postgres-only. SQLite tests recreate the enum from the model on each run.
Note: Postgres `ALTER TYPE ... ADD VALUE` is not reversible; downgrade is a no-op.
Removing a value would require rewriting the type and updating every row.
"""

import os

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified_type(name: str) -> str:
    return f"{_schema}.{name}" if _schema else name


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # ADD VALUE IF NOT EXISTS is supported from Postgres 9.6+; idempotent across re-runs.
    op.execute(
        f"ALTER TYPE {_qualified_type('reservationstatus')} "
        f"ADD VALUE IF NOT EXISTS 'PENDING_PROVISION'"
    )
    op.execute(
        f"ALTER TYPE {_qualified_type('reservationstatus')} ADD VALUE IF NOT EXISTS 'FAILED'"
    )


def downgrade() -> None:
    # Irreversible: removing enum values in Postgres requires rewriting the type
    # and rewriting any rows that reference the removed values. Left as a no-op.
    pass
