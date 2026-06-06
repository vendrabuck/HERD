"""Create execution_runs and driver_cache tables.

Revision ID: 0001
Revises:
Create Date: 2026-03-28 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")

    op.create_table(
        "execution_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("device_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("driver_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("driver_sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("input_params", sa.JSON(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("port_a", sa.String(255), nullable=True),
        sa.Column("port_b", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )

    op.create_table(
        "driver_cache",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("driver_id", sa.Uuid(as_uuid=True), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("local_path", sa.String(512), nullable=False),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("driver_cache", schema=_schema)
    op.drop_table("execution_runs", schema=_schema)
