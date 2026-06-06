"""Add device_config_apply_jobs table for scheduled config pushes.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-02 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_devices_fk = f"{_schema}.devices.id" if _schema else "devices.id"
_versions_fk = f"{_schema}.device_config_versions.id" if _schema else "device_config_versions.id"


def upgrade() -> None:
    op.create_table(
        "device_config_apply_jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_devices_fk, ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "version_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_versions_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("author_name", sa.String(150), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_apply_jobs_status_scheduled",
        "device_config_apply_jobs",
        ["status", "scheduled_for"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_apply_jobs_status_scheduled",
        table_name="device_config_apply_jobs",
        schema=_schema,
    )
    op.drop_table("device_config_apply_jobs", schema=_schema)
