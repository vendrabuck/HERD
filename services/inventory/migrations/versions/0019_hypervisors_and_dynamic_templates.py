"""Add hypervisors table and device_templates.hypervisor_id.

Backs the dynamic-resource feature (issue #32, ADR 0004). A hypervisor is a
registered backend (proxmox, vsphere, libvirt) whose credential lives in the
secrets service, referenced here only by a bare secret_id UUID. A dynamic
template names both a recipe driver and a hypervisor; the new nullable
hypervisor_id FK carries that reference.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-03 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "hypervisors",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("hypervisor_type", sa.String(50), nullable=False),
        sa.Column("secret_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("modified_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )

    hypervisors_ref = f"{_schema}.hypervisors" if _schema else "hypervisors"
    op.add_column(
        "device_templates",
        sa.Column(
            "hypervisor_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{hypervisors_ref}.id"),
            nullable=True,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_templates", "hypervisor_id", schema=_schema)
    op.drop_table("hypervisors", schema=_schema)
