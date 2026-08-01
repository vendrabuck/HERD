"""Add reservations.pending_fork_prune_device_ids.

Issue #462: the device-set PATCH records the removed device ids in this nullable
JSON column (a list of UUID strings) atomically with the edit, so a fork prune
that fails or crashes after the commit is durably visible; the expiration sweep's
pending-prune reconciler retries it until cabling converges, then clears the ids.
Null means no prune is pending.

Column-add only, so the create_all-vs-migration hazard (issue #419) does not
apply: schema_init's create_all never adds columns to an existing table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-01 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("pending_fork_prune_device_ids", sa.JSON(), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservations", "pending_fork_prune_device_ids", schema=_schema)
