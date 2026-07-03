"""Create the secrets schema: key_versions and secrets (issue #39, ADR 0003).

key_versions holds each data-encryption key wrapped (AES-GCM) by the
environment-supplied KEK; secrets holds plaintext metadata plus the encrypted
payload (ciphertext, nonce, key_version). No plaintext secret material is ever
stored.

Revision ID: 0001
Revises:
Create Date: 2026-07-03 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _fk_target(table: str, column: str) -> str:
    return f"{_schema}.{table}.{column}" if _schema else f"{table}.{column}"


def upgrade() -> None:
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")

    op.create_table(
        "key_versions",
        sa.Column("version", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )

    op.create_table(
        "secrets",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column(
            "key_version",
            sa.Integer(),
            sa.ForeignKey(_fk_target("key_versions", "version")),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(as_uuid=True), nullable=True),
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
        "ix_secrets_name",
        "secrets",
        ["name"],
        unique=True,
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("ix_secrets_name", table_name="secrets", schema=_schema)
    op.drop_table("secrets", schema=_schema)
    op.drop_table("key_versions", schema=_schema)
