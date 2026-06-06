"""Add device_config_versions table and devices.current_config_version_id pointer.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-02 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_devices_fk = f"{_schema}.devices.id" if _schema else "devices.id"
_versions_fk = f"{_schema}.device_config_versions.id" if _schema else "device_config_versions.id"


def upgrade() -> None:
    op.create_table(
        "device_config_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_devices_fk, ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("connection_type", sa.String(64), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_name", sa.String(150), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "restored_from_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_versions_fk, ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_apply_run_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_device_config_versions_device_version",
        "device_config_versions",
        ["device_id", "version_number"],
        unique=True,
        schema=_schema,
    )
    op.add_column(
        "devices",
        sa.Column(
            "current_config_version_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_versions_fk, ondelete="SET NULL"),
            nullable=True,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("devices", "current_config_version_id", schema=_schema)
    op.drop_index(
        "ix_device_config_versions_device_version",
        table_name="device_config_versions",
        schema=_schema,
    )
    op.drop_table("device_config_versions", schema=_schema)
