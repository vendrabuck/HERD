"""Create topology_templates table for roadmap item #8 iteration 2.

Revision ID: 0006
Revises: 0005
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "topology_templates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("canvas_data", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("owner_name", sa.String(150), nullable=False, server_default=""),
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
        sa.UniqueConstraint("name", name="uq_topology_templates_name"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("topology_templates", schema=_schema)
