"""Add claimed_until to the three wiring ledgers.

Issue #817. The two wiring retry channels (the manual endpoint's
reattempt_reservation and the background run_wiring_retry_tick) could load the
same FAILED row and each call the driver for it: issue #814 made the failure
WRITE a row-identity compare-and-swap, so the ledger can no longer be corrupted,
but nothing claimed a row before it was driven. This column is that claim: a
retry stamps `claimed_until = now + budget` in a compare-and-swap immediately
before the row's own driver call, and every record path clears it again. A
process that dies mid-drive leaves a stamp that expires by itself, so there is
no reaper and no heartbeat.

Purely additive and nullable: NULL means unclaimed, which is what every existing
row means, so no backfill step is needed. Bare op.add_column is safe on
upgraded-in-place stacks (herd_common.schema_init.create_all_and_stamp skips
create_all on a stamped schema, so a per-migration has_table guard is not
required for new migrations).

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-14 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None

_TABLES = ("l1_connection_assignments", "l2_port_assignments", "route_assignments")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
            schema=_schema,
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "claimed_until", schema=_schema)
