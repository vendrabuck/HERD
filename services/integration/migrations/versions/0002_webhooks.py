"""Add webhook_subscriptions and webhook_deliveries (issue #33, phase 4).

Outbound integration webhooks: an admin registers a subscription (target URL,
subscribed reservation event types, shared HMAC secret) and the NATS consumer
fans matching reservation lifecycle events out to each one. webhook_deliveries
is the at-least-once dedup ledger and the dead-letter record on exhaustion.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-29 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _fk_target(table: str) -> str:
    return f"{_schema}.{table}.id" if _schema else f"{table}.id"


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("event_types", JSONB(), nullable=False),
        sa.Column("secret", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(_fk_target("webhook_subscriptions"), ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_id",
        "webhook_deliveries",
        ["subscription_id"],
        schema=_schema,
    )
    op.create_index(
        "uq_webhook_delivery_subscription_event",
        "webhook_deliveries",
        ["subscription_id", "event_id"],
        unique=True,
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_webhook_delivery_subscription_event",
        table_name="webhook_deliveries",
        schema=_schema,
    )
    op.drop_index(
        "ix_webhook_deliveries_subscription_id",
        table_name="webhook_deliveries",
        schema=_schema,
    )
    op.drop_table("webhook_deliveries", schema=_schema)
    op.drop_table("webhook_subscriptions", schema=_schema)
