"""Create fork_l3_routes for ADR 0014 phase 1 (issue #34).

Purely additive: no existing table changes and nothing to backfill (the ADR
retired the "existing minimal L3 data migrates" acceptance criterion, since
today's L3 layer flag on a physical Connection carries no routing intent to
carry forward). Downgrade drops the one new table. See
docs/design/0014-first-class-layer-3-routing.md (Decision 1).

Revision ID: 0011
Revises: 0010
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    op.create_table(
        "fork_l3_routes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "fork_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('reservation_fork')}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("destination", sa.String(64), nullable=False),
        sa.Column("next_hop", sa.String(64), nullable=True),
        sa.Column("interface", sa.String(64), nullable=False),
        sa.Column("virtual_router", sa.String(64), nullable=True),
        sa.Column("route_key", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(150), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "fork_id",
            "device_id",
            "route_key",
            name="uq_fork_l3_routes_device_route",
        ),
        schema=_schema,
    )
    op.create_index("ix_fork_l3_routes_fork_id", "fork_l3_routes", ["fork_id"], schema=_schema)


def downgrade() -> None:
    op.drop_index("ix_fork_l3_routes_fork_id", table_name="fork_l3_routes", schema=_schema)
    op.drop_table("fork_l3_routes", schema=_schema)
