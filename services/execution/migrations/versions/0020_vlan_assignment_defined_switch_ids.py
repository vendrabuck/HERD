"""Add defined_switch_ids to vlan_assignments (issue #442, VLAN definition lifecycle).

Option B (decided 2026-08-01): VLAN definition lifecycle is HERD-owned and coupled
to the allocation lifecycle. This column records the switches on which create_vlan
has confirmed success for an allocation; the last-free delete_vlan pass targets it.
Purely additive, server_default '[]': every pre-#442 allocation backfills to an
EMPTY defined set, so upgraded stacks drive no delete_vlan for VLANs this code
cannot prove it created (their lingering matches the pre-#442 phase 6/7 boundary).
switch_device_ids changes meaning (add switches only, to the transit-inclusive
definition scope) without a schema change; the first fork-driven reconcile after
upgrade refreshes it in place.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-01 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "vlan_assignments",
        sa.Column("defined_switch_ids", sa.JSON(), nullable=False, server_default="[]"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("vlan_assignments", "defined_switch_ids", schema=_schema)
