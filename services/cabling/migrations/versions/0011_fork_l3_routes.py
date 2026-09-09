"""Create fork_l3_routes for ADR 0014 phase 1 (issue #34).

Purely additive: no existing table changes and nothing to backfill (the ADR
retired the "existing minimal L3 data migrates" acceptance criterion, since
today's L3 layer flag on a physical Connection carries no routing intent to
carry forward). Downgrade drops the one new table. See
docs/design/0014-first-class-layer-3-routing.md (Decision 1).

This revision is still unreleased on the feat/34-l3-intent-cabling branch as of
the round-2 adversarial review, so its two review fixes (S4, S6) are folded in
place here rather than added as a follow-up revision: `route_key` widens from
String(200) to String(400) (S4: the identity now JSON-packs all four fields,
including `virtual_router`, instead of `|`-joining three), and the nullable
`validated_config_version_id` column is added (S6: records the inventory config
version the save-time L3 validation pass actually judged the route against, for
phase 3 to compare against the switch's current version before driving).

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
        sa.Column("route_key", sa.String(400), nullable=False),
        sa.Column("validated_config_version_id", sa.Uuid(as_uuid=True), nullable=True),
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
