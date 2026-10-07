"""Add hypervisors.device_group_id: who may book the hypervisor's dynamic templates.

Issue #1053. A non-admin may book a dynamic template only when it is visible to
them, and a dynamic template is visible through its hypervisor: the hypervisor
names one device group, and every user group with a permission on that device
group may see and book the templates that point at the hypervisor (the same
DeviceGroupPermission rows that grant physical devices). A null value means no
non-admin may see the hypervisor's templates, which is also the value every
hypervisor registered before this revision gets, so the gate is closed until an
admin assigns a group.

The foreign key sets the column to null when the device group is deleted,
closing the gate rather than leaving a dangling id.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-07 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    device_groups_ref = f"{_schema}.device_groups" if _schema else "device_groups"
    op.add_column(
        "hypervisors",
        sa.Column(
            "device_group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                f"{device_groups_ref}.id",
                ondelete="SET NULL",
                name="fk_hypervisors_device_group_id",
            ),
            nullable=True,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_hypervisors_device_group_id", "hypervisors", type_="foreignkey", schema=_schema
    )
    op.drop_column("hypervisors", "device_group_id", schema=_schema)
