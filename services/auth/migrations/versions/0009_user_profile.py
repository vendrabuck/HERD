"""Add the user profile columns: full_name, contact_email, phone (issue #1252).

full_name is added nullable, backfilled from username, then made NOT NULL, so
the constraint lands on a table that already holds rows. contact_email and
phone stay nullable.

Revision ID: 0009
Revises: 0008
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(200), nullable=True), schema=_schema)
    op.add_column(
        "users", sa.Column("contact_email", sa.String(255), nullable=True), schema=_schema
    )
    op.add_column("users", sa.Column("phone", sa.String(32), nullable=True), schema=_schema)
    users = sa.table("users", sa.column("full_name"), sa.column("username"), schema=_schema)
    op.execute(users.update().values(full_name=users.c.username))
    with op.batch_alter_table("users", schema=_schema) as batch:
        batch.alter_column("full_name", existing_type=sa.String(200), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=_schema) as batch:
        batch.drop_column("phone")
        batch.drop_column("contact_email")
        batch.drop_column("full_name")
