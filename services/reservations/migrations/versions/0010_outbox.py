"""Add the transactional outbox table (issue #21).

Durable, at-least-once event delivery: each reservation state change writes an
outbox row in the SAME transaction as the change, so the event exists iff the
transition committed. A background relay (app.main) publishes unpublished rows to
JetStream and marks them sent, retrying across a messaging outage. This replaces
the old fire-and-forget post-commit NATS publish that could drop an event if the
process died or NATS was unreachable between the commit and the publish.

The column shape mirrors herd_common.outbox.OutboxMixin. payload is JSONB on
Postgres (queryable, compact) and plain JSON on the SQLite unit-test backend.
published_at is indexed so the relay's `published_at IS NULL` claim and the
prune's `published_at < cutoff` scan stay cheap as the table grows.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-28 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    payload_type = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("payload", payload_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        schema=_schema,
    )
    op.create_index(
        "ix_outbox_published_at",
        "outbox",
        ["published_at"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_published_at", table_name="outbox", schema=_schema)
    op.drop_table("outbox", schema=_schema)
