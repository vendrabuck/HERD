"""Create reservation_fork, fork_connections, fork_versions for issue #25.

Purely additive: the physical connections table is untouched, so there is nothing
to backfill and downgrade simply drops the three new tables. See
docs/design/0001-editable-reservation-topologies.md (Decision 1, Migration shape).

Revision ID: 0007
Revises: 0006
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    op.create_table(
        "reservation_fork",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        # Bare reservation UUID; no cross-schema FK. Unique + indexed so a retried
        # activation is idempotent on reservation_id.
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_topology_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("parent_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("canvas_data", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("reservation_id", name="uq_reservation_fork_reservation"),
        schema=_schema,
    )
    op.create_index(
        "ix_reservation_fork_reservation_id",
        "reservation_fork",
        ["reservation_id"],
        schema=_schema,
    )

    op.create_table(
        "fork_connections",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "fork_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('reservation_fork')}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_a_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("port_a", sa.String(255), nullable=False),
        sa.Column("device_b_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("port_b", sa.String(255), nullable=False),
        sa.Column("layer", sa.String(10), nullable=False),
        sa.Column("physical_connection_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.String(150), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "fork_id",
            "device_a_id",
            "port_a",
            "device_b_id",
            "port_b",
            "layer",
            name="uq_fork_connections_endpoints",
        ),
        schema=_schema,
    )
    op.create_index("ix_fork_connections_fork_id", "fork_connections", ["fork_id"], schema=_schema)
    op.create_index(
        "ix_fork_connections_device_a_id",
        "fork_connections",
        ["device_a_id"],
        schema=_schema,
    )
    op.create_index(
        "ix_fork_connections_device_b_id",
        "fork_connections",
        ["device_b_id"],
        schema=_schema,
    )

    op.create_table(
        "fork_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "fork_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('reservation_fork')}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("canvas_data", sa.JSON(), nullable=True),
        sa.Column(
            "restored_from_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('fork_versions')}.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("fork_id", "version_number", name="uq_fork_versions_fork_version"),
        schema=_schema,
    )
    op.create_index("ix_fork_versions_fork_id", "fork_versions", ["fork_id"], schema=_schema)


def downgrade() -> None:
    op.drop_table("fork_versions", schema=_schema)
    op.drop_table("fork_connections", schema=_schema)
    op.drop_table("reservation_fork", schema=_schema)
