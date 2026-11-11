"""Add the OIDC flow store and federated identities (ADR 0016 phase 2, issue #37).

Two new tables, no change to an existing one and no data migration:
oidc_login_flows (D2) and federated_identities (D4). Every constraint and index
is named, and the names equal the models' (app/models/oidc_login_flow.py,
app/models/federated_identity.py), so a schema built by create_all and one
built by this chain are the same; services/auth/tests/test_oidc_sso_migration.py
compares the two.

Revision ID: 0010
Revises: 0009
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_users_id = f"{_schema}.users.id" if _schema else "users.id"

_FLOW_STATUS_CHECK = "status IN ('pending', 'claimed', 'authenticated', 'exchanged', 'failed')"


def upgrade() -> None:
    op.create_table(
        "oidc_login_flows",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=True),
        sa.Column("next_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("handoff_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handoff_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_users_id],
            ondelete="CASCADE",
            name="fk_oidc_login_flows_user_id_users",
        ),
        sa.UniqueConstraint("state_hash", name="uq_oidc_login_flows_state_hash"),
        sa.UniqueConstraint("handoff_hash", name="uq_oidc_login_flows_handoff_hash"),
        sa.CheckConstraint(_FLOW_STATUS_CHECK, name="ck_oidc_login_flows_status"),
        schema=_schema,
    )
    op.create_index(
        "ix_oidc_login_flows_expires_at",
        "oidc_login_flows",
        ["expires_at"],
        unique=False,
        schema=_schema,
    )
    op.create_table(
        "federated_identities",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_users_id],
            ondelete="CASCADE",
            name="fk_federated_identities_user_id_users",
        ),
        sa.UniqueConstraint("user_id", name="uq_federated_identities_user_id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_federated_identities_issuer_subject"),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("federated_identities", schema=_schema)
    op.drop_index("ix_oidc_login_flows_expires_at", table_name="oidc_login_flows", schema=_schema)
    op.drop_table("oidc_login_flows", schema=_schema)
