"""Add the fork_wiring_ledger table (issue #345 P3b phase 2, ADR 0007).

One row per reservation records last_staged_fork_version, the fork_version of the
last reservation.wiring_changed event this service staged. It is upserted in the SAME
transaction as the outbox enqueue, so the event exists iff the ledger advanced (ADR
0007 Decision 2). The standing sweeper compares cabling's latest fork_version against
this row to heal a missed staging.

No cross-schema FK: reservation_id is a bare UUID primary key keyed to reservations'
own rows, matching the schema-per-service constraint.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-17 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "fork_wiring_ledger",
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("last_staged_fork_version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("fork_wiring_ledger", schema=_schema)
