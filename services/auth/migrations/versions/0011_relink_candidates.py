"""Add the single sign-on relink candidates (issue #1399, owner decision 2026-11-18).

Two new tables, no change to an existing one and no data migration:
federated_relink_candidates (the refused (issuer, subject) of a login whose
email an `oidc` account holds) and federated_relink_capture_totals (a per-account
count of the distinct pairs ever captured). Every constraint and index is named,
and the names equal the models' (app/models/federated_relink_candidate.py), so a
schema built by create_all and one built by this chain are the same;
services/auth/tests/test_relink_candidates_migration.py compares the two.

Revision ID: 0011
Revises: 0010
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None
_users_id = f"{_schema}.users.id" if _schema else "users.id"


def upgrade() -> None:
    op.create_table(
        "federated_relink_candidates",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_users_id],
            ondelete="CASCADE",
            name="fk_federated_relink_candidates_user_id_users",
        ),
        sa.UniqueConstraint(
            "user_id", "issuer", "subject", name="uq_federated_relink_candidates_user_pair"
        ),
        schema=_schema,
    )
    op.create_index(
        "ix_federated_relink_candidates_created_at",
        "federated_relink_candidates",
        ["created_at"],
        unique=False,
        schema=_schema,
    )
    op.create_table(
        "federated_relink_capture_totals",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("captured_total", sa.Integer(), nullable=False),
        sa.Column("last_captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_users_id],
            ondelete="CASCADE",
            name="fk_federated_relink_capture_totals_user_id_users",
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_table("federated_relink_capture_totals", schema=_schema)
    op.drop_index(
        "ix_federated_relink_candidates_created_at",
        table_name="federated_relink_candidates",
        schema=_schema,
    )
    op.drop_table("federated_relink_candidates", schema=_schema)
