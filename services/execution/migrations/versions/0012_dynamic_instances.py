"""Create dynamic_instances table for hypervisor-backed instance provisioning.

The applied-state ledger for ADR 0004 (issue #32): one row per requested
dynamic instance, keyed by the booking's request_id (unique) so a NATS
redelivery of reservation.provision_requested reuses the row rather than
double-creating an instance. Teardown drives strictly from these rows, the
peer of vlan_assignments and route_assignments.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-03 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "dynamic_instances",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("template_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("hypervisor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("instance_ref", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, default="CREATING"),
        sa.Column("error", sa.Text(), nullable=True),
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
        schema=_schema,
    )

    op.create_index(
        "uq_dynamic_instances_request_id",
        "dynamic_instances",
        ["request_id"],
        unique=True,
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_dynamic_instances_request_id",
        table_name="dynamic_instances",
        schema=_schema,
    )
    op.drop_table("dynamic_instances", schema=_schema)
