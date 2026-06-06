"""Add outbound_deliveries idempotency ledger.

Outbound channels (email, chat, webhook) have no persisted notification row to
collide on, so a redelivered NATS message would resend. This table records that
an outbound send already happened for a (channel, user_id, dedupe_key); a
partial-unique index where dedupe_key is not null makes a redelivery a no-op
per channel and recipient. The row is committed only after the side effect
succeeds, so a failed send is retried on the next delivery.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.create_table(
        "outbound_deliveries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "uq_outbound_channel_user_dedupe",
        "outbound_deliveries",
        ["channel", "user_id", "dedupe_key"],
        unique=True,
        schema=_schema,
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_outbound_channel_user_dedupe",
        table_name="outbound_deliveries",
        schema=_schema,
    )
    op.drop_table("outbound_deliveries", schema=_schema)
