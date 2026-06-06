"""User groups and group members tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-13 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "user_groups",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                f"{_schema}.users.id" if _schema else "users.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        schema=_schema,
    )

    op.create_table(
        "group_members",
        sa.Column(
            "group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                f"{_schema}.user_groups.id" if _schema else "user_groups.id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                f"{_schema}.users.id" if _schema else "users.id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("group_members", schema=_schema)
    op.drop_table("user_groups", schema=_schema)
