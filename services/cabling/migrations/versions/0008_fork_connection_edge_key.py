"""Add edge_key to fork_connections for per-edge hop grouping (issue #345 P3b).

Purely additive: a single nullable column recording the React Flow canvas edge id a
hop was resolved from, so the execution consumer can group flattened switch-touching
hops back by their originating canvas edge. Existing rows backfill as NULL (unknown
originating edge); consumers treat NULL as ungrouped. edge_key is deliberately NOT part
of the connection identity (ADR 0006 Decision 3), so no unique constraint changes.
Downgrade drops the column.

Revision ID: 0008
Revises: 0007
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
    op.add_column(
        "fork_connections",
        sa.Column("edge_key", sa.String(255), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("fork_connections", "edge_key", schema=_schema)
