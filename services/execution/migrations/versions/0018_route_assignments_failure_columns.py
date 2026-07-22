"""Add attempts, last_error, and intended to route_assignments.

ADR 0009 Decision 2 (docs/design/0009-l2-l3-connection-driven-reconcile.md), issue
#369/#416. Purely additive: `status` already accepts any string value, so no
migration step is needed to make "FAILED" legal (the model docstring documents it
as a new legal value); this revision only adds the columns that value needs.
route_service does not write these columns yet (that is the L3 layered reconcile,
ADR 0009 phase 5) - no behavior change this phase beyond accepting them.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-19 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "route_assignments",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        schema=_schema,
    )
    op.add_column(
        "route_assignments",
        sa.Column("last_error", sa.Text(), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "route_assignments",
        sa.Column("intended", sa.String(20), nullable=False, server_default="ACTIVE"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("route_assignments", "intended", schema=_schema)
    op.drop_column("route_assignments", "last_error", schema=_schema)
    op.drop_column("route_assignments", "attempts", schema=_schema)
