"""Partial index on reservation_fork for the ACTIVE listing (issue #710).

`GET /internal/forks` (routes/forks.py) filters on status = 'ACTIVE' and orders by
created_at; reservations drains every page of it every expiration-sweep tick (60s in
prod, 5s in dev), and fork rows are never deleted (archived forks are the as-built
record, read by the transit report), so the table grows forever by design while the
listing stays unindexed. A partial index scoped to the ACTIVE rows only (most forks
end up ARCHIVED once their reservation completes, so the live set the sweep actually
walks stays small even as the full table grows) covers both the WHERE and the ORDER
BY in one structure. `(created_at, id)` rather than `created_at` alone: id is the
listing's stable pagination tie-breaker for rows sharing a created_at timestamp.

Purely additive: no data changes, downgrade drops the index.

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


def upgrade() -> None:
    op.create_index(
        "ix_reservation_fork_active_created_at",
        "reservation_fork",
        ["created_at", "id"],
        schema=_schema,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservation_fork_active_created_at",
        table_name="reservation_fork",
        schema=_schema,
    )
