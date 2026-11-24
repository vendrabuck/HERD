"""Rewrite empty pending fork-prune markers to SQL NULL (issue #1522).

Migration 0014 added reservations.pending_fork_prune_device_ids as plain JSON, and
the model declared it without `none_as_null=True`, so every marker the service
cleared (`remaining or None`) was stored as the JSON literal "null", not SQL NULL.
The expiration sweep's pending-prune reconciler selects `IS NOT NULL` rows, oldest
updated_at first, 20 per tick, so those cleared rows stayed selected forever and,
once 20 of them existed, a new marker was never retried. The model now stores SQL
NULL; this one-time data pass repairs the rows written before that.

The stored states are exactly two after it: SQL NULL (nothing pending) or a
non-empty list. Both empty forms become SQL NULL: the JSON "null" every clear
wrote, and an empty list "[]", which no code path writes (the PATCH unions at
least one id, a clear writes `remaining or None`) but which the reconciler would
also select forever without pruning anything. The match is by JSON value on
Postgres (a jsonb comparison, so spacing in the stored text does not matter) and
by normalized JSON text elsewhere (SQLite, the unit tests' engine).

Data-only and safe to run twice: a second run matches no row. Rows holding SQL
NULL or a non-empty list are not touched.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-24 00:00:00.000000
"""

import os

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        empty = "pending_fork_prune_device_ids::jsonb IN ('null'::jsonb, '[]'::jsonb)"
    else:
        empty = "json(pending_fork_prune_device_ids) IN ('null', '[]')"
    op.execute(
        f"UPDATE {_qualified('reservations')} "
        f"SET pending_fork_prune_device_ids = NULL "
        f"WHERE pending_fork_prune_device_ids IS NOT NULL AND {empty}"
    )


def downgrade() -> None:
    # Data-only migration with no honest inverse: which rows held JSON "null"
    # before is not recorded, and writing it back would restore the defect. The
    # code before this revision reads SQL NULL as "nothing pending" exactly as it
    # read JSON "null", so leaving the rows as they are is correct for it. No-op.
    pass
