"""Add api_tokens table for machine-principal (API token) authentication.

Revision ID: 0005
Revises: 0004
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("principal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        # Reuse the existing "role" enum type created in 0001; do not recreate it.
        sa.Column(
            "role",
            sa.Enum("USER", "ADMIN", "SUPERADMIN", name="role", schema=_schema, create_type=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            [f"{_schema}.users.id" if _schema else "users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=_schema,
    )
    op.create_index(
        "ix_api_tokens_token_hash",
        "api_tokens",
        ["token_hash"],
        unique=True,
        schema=_schema,
    )
    op.create_index(
        "ix_api_tokens_principal_id",
        "api_tokens",
        ["principal_id"],
        unique=False,
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("ix_api_tokens_principal_id", table_name="api_tokens", schema=_schema)
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens", schema=_schema)
    op.drop_table("api_tokens", schema=_schema)
