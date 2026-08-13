"""Add ldap_group_mappings for directory group sync (ADR 0011 phase 2).

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
        "ldap_group_mappings",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("group_dn", sa.Text(), nullable=False),
        sa.Column("directory_name", sa.String(length=255), nullable=False),
        sa.Column("herd_group_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["herd_group_id"],
            [f"{_schema}.user_groups.id" if _schema else "user_groups.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            [f"{_schema}.users.id" if _schema else "users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_dn", name="uq_ldap_group_mappings_group_dn"),
        # One directory group per HERD group: per-mapping reconcile set
        # arithmetic cannot converge when two mappings share a target.
        sa.UniqueConstraint("herd_group_id", name="uq_ldap_group_mappings_herd_group_id"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("ldap_group_mappings", schema=_schema)
