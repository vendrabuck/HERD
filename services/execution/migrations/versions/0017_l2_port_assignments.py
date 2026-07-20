"""Create l2_port_assignments.

ADR 0009 Decision 2 (docs/design/0009-l2-l3-connection-driven-reconcile.md), issue
#369/#416. Schema only: this table is not read or written by any service code in
this revision. It is the per-port L2 VLAN membership ledger the layered reconcile
(ADR 0009 phase 4) will use, split from vlan_assignments' per-fabric VLAN-number
allocation exactly as l1_connection_assignments (migration 0014) split the
per-connection applied state from execution_runs' audit log.

The partial-unique index allows at most one ACTIVE membership per
(switch_device_id, port, vlan_assignment_id); RELEASED and FAILED rows are
retained and never block a new claim, mirroring uq_l1_active_per_switch_pair and
uq_vlan_active_per_fabric. The FAILED-partial index on created_at mirrors
migration 0015's ix_l1_connection_assignments_failed_created_at (issue #390
pattern): a future retry sweep's per-tick candidate query needs it, and the table
is append-only by the same design (FAILED/RELEASED rows are the applied-wiring
audit trail).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-19 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "l2_port_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("vlan_assignment_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("switch_device_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("port", sa.String(128), nullable=False),
        sa.Column("intended", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )

    op.create_index(
        "uq_l2_active_per_switch_port_vlan",
        "l2_port_assignments",
        ["switch_device_id", "port", "vlan_assignment_id"],
        unique=True,
        schema=_schema,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_index(
        "ix_l2_port_assignments_failed_created_at",
        "l2_port_assignments",
        ["created_at"],
        schema=_schema,
        postgresql_where=sa.text("status = 'FAILED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_l2_port_assignments_failed_created_at",
        table_name="l2_port_assignments",
        schema=_schema,
    )
    op.drop_index(
        "uq_l2_active_per_switch_port_vlan",
        table_name="l2_port_assignments",
        schema=_schema,
    )
    op.drop_table("l2_port_assignments", schema=_schema)
