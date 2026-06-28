"""Create the transactional outbox table (issue #21).

The health scheduler enqueues a `device.health_transition` event row in the
same transaction as the device health-status update, so the event exists iff
the transition committed. A background relay (execution/app/main.py) publishes
unpublished rows to JetStream and marks them sent, then prunes old published
rows. This closes the dual-write gap where a status change committed but its
NATS publish was lost to a crash or a messaging outage.

Adds:
- outbox.id (UUID pk), subject, payload (JSONB on Postgres, JSON elsewhere),
  created_at, published_at (nullable), attempts.
- ix_outbox_published_at: serves the relay's "published_at IS NULL" claim and
  the prune's "published_at < cutoff" scan as an index scan.

Column shape matches herd_common.outbox.OutboxMixin.

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

_json_type = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("payload", _json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        schema=_schema,
    )
    op.create_index(
        "ix_outbox_published_at",
        "outbox",
        ["published_at"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_published_at",
        table_name="outbox",
        schema=_schema,
    )
    op.drop_table("outbox", schema=_schema)
