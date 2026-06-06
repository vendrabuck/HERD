"""Partial-unique index for at most one ACTIVE VLAN per (fabric, vlan_id).

Closes a check-then-act race in find_or_assign_vlan: two concurrent callers in
the same fabric could read the same in-use VLAN set and both insert the same
VLAN. The predicate is ACTIVE-only so RELEASED rows do not block VLAN reuse.

Note: this upgrade fails if duplicate ACTIVE (fabric_id, vlan_id) rows already
exist. None are expected; if it errors, deduplicate before re-running.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-31 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_index(
        "uq_vlan_active_per_fabric",
        "vlan_assignments",
        ["fabric_id", "vlan_id"],
        unique=True,
        schema=_schema,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_vlan_active_per_fabric",
        table_name="vlan_assignments",
        schema=_schema,
    )
