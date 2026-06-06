"""Add dedupe_key for notification idempotency.

Adds a nullable dedupe_key column (the source NATS stream+sequence, e.g.
"HERD_RESERVATIONS:42") and a partial-unique index on (user_id, dedupe_key)
where dedupe_key is not null. A redelivered NATS message keeps the same
sequence, so the unique index makes a redelivery a no-op per recipient. Rows
without a key (legacy or non-event-sourced) stay unconstrained.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-31 00:00:00.000000
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
    op.add_column(
        "notifications",
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "uq_notifications_user_dedupe",
        "notifications",
        ["user_id", "dedupe_key"],
        unique=True,
        schema=_schema,
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_notifications_user_dedupe", table_name="notifications", schema=_schema)
    op.drop_column("notifications", "dedupe_key", schema=_schema)
