"""Create resource_grants table.

Revision ID: 0001
Revises:
Create Date: 2026-03-14 00:00:00.000000
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
        "resource_grants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("group_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("permission", sa.String(50), nullable=False),
        sa.Column("granted_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "group_id",
            "resource_type",
            "resource_id",
            "permission",
            name="uq_grant_group_resource_permission",
        ),
        sa.Index("ix_grant_resource", "resource_type", "resource_id"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("resource_grants", schema=_schema)
