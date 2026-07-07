"""recipe_drafts table for AI-assisted recipe authoring (ADR 0005, issue #28).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-07 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "recipe_drafts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("hypervisor_type", sa.Text, nullable=True),
        sa.Column("driver_py", sa.Text, nullable=False),
        sa.Column("driver_metadata_json", sa.Text, nullable=False),
        sa.Column("explanation", sa.Text, nullable=True),
        sa.Column("validation_json", sa.Text, nullable=True),
        sa.Column("valid", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model", sa.Text, nullable=True),
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
        "ix_recipe_drafts_user_created",
        "recipe_drafts",
        ["user_id", "created_at"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("ix_recipe_drafts_user_created", table_name="recipe_drafts", schema=_schema)
    op.drop_table("recipe_drafts", schema=_schema)
