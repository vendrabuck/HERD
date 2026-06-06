"""Redesign drivers as standalone entities.

Remove device_id FK from driver_packages, add name/description.
Replace driver_type on device_templates with driver_id FK.

Revision ID: 0008
Revises: 0007
Create Date: 2026-03-15 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    # 1. Add name and description to driver_packages
    op.add_column(
        "driver_packages",
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        schema=_schema,
    )
    op.add_column(
        "driver_packages",
        sa.Column("description", sa.Text, nullable=True),
        schema=_schema,
    )

    # 2. Drop device_id FK and unique constraint from driver_packages
    if _schema:
        op.drop_constraint(
            "driver_packages_device_id_fkey", "driver_packages", type_="foreignkey", schema=_schema
        )
        op.drop_constraint(
            "driver_packages_device_id_key", "driver_packages", type_="unique", schema=_schema
        )
    else:
        # SQLite: constraints are part of table, but Alembic batch mode handles this
        with op.batch_alter_table("driver_packages", schema=_schema) as batch_op:
            batch_op.drop_constraint("driver_packages_device_id_fkey", type_="foreignkey")
            batch_op.drop_constraint("driver_packages_device_id_key", type_="unique")
    op.drop_column("driver_packages", "device_id", schema=_schema)

    # 3. Add unique constraint on driver_packages.name
    op.create_unique_constraint(
        "driver_packages_name_key", "driver_packages", ["name"], schema=_schema
    )

    # 4. Remove server_default from name (was only for migration)
    if _schema:
        op.alter_column("driver_packages", "name", server_default=None, schema=_schema)
    else:
        with op.batch_alter_table("driver_packages", schema=_schema) as batch_op:
            batch_op.alter_column("name", server_default=None)

    # 5. Add driver_id FK to device_templates
    driver_packages_ref = f"{_schema}.driver_packages" if _schema else "driver_packages"
    op.add_column(
        "device_templates",
        sa.Column(
            "driver_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{driver_packages_ref}.id"),
            nullable=True,
        ),
        schema=_schema,
    )

    # 6. Drop driver_type from device_templates
    op.drop_column("device_templates", "driver_type", schema=_schema)


def downgrade() -> None:
    # Re-add driver_type
    op.add_column(
        "device_templates",
        sa.Column("driver_type", sa.String(50), nullable=True),
        schema=_schema,
    )

    # Drop driver_id
    op.drop_column("device_templates", "driver_id", schema=_schema)

    # Drop unique constraint on name
    op.drop_constraint(
        "driver_packages_name_key", "driver_packages", type_="unique", schema=_schema
    )

    # Drop name and description
    op.drop_column("driver_packages", "description", schema=_schema)
    op.drop_column("driver_packages", "name", schema=_schema)

    # Re-add device_id
    devices_ref = f"{_schema}.devices" if _schema else "devices"
    op.add_column(
        "driver_packages",
        sa.Column(
            "device_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{devices_ref}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_unique_constraint(
        "driver_packages_device_id_key", "driver_packages", ["device_id"], schema=_schema
    )
