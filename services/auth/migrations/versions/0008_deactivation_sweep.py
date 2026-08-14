"""Add deactivated_by_sync and sweep counters for the deactivation and
reactivation sweep (ADR 0011 phase 4).

Revision ID: 0008
Revises: 0007
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "deactivated_by_sync",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema=_schema,
    )
    op.add_column(
        "ldap_sync_runs",
        sa.Column(
            "users_deactivated",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        schema=_schema,
    )
    op.add_column(
        "ldap_sync_runs",
        sa.Column(
            "users_reactivated",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("ldap_sync_runs", "users_reactivated", schema=_schema)
    op.drop_column("ldap_sync_runs", "users_deactivated", schema=_schema)
    op.drop_column("users", "deactivated_by_sync", schema=_schema)
