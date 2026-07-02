"""Create route_assignments table for L3 route provisioning state.

Pins the exact route set applied to an L3 switch when a reservation is
provisioned, so deprovision removes exactly that set instead of re-deriving
from a config that may have been edited mid-reservation (issue #20). The
partial-unique index allows at most one ACTIVE assignment per
(reservation, device); RELEASED rows are retained as history and never block
a new assignment.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-02 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "route_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("device_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("routes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, default="ACTIVE"),
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
        "uq_route_active_per_res_device",
        "route_assignments",
        ["reservation_id", "device_id"],
        unique=True,
        schema=_schema,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_route_active_per_res_device",
        table_name="route_assignments",
        schema=_schema,
    )
    op.drop_table("route_assignments", schema=_schema)
