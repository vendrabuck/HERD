"""Add the reservation_dynamic_requests table (ADR 0004, issue #32).

One row per requested dynamic instance on a reservation. The row id is the
request_id the execution service keys its create idempotency on, minted once at
booking time so it is stable across provision_requested redeliveries.
template_id is a bare UUID (no cross-schema FK; templates live in the inventory
schema and are validated over HTTP at booking time). reservation_id cascades on
delete with its parent reservation.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-05 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _qualified(table: str) -> str:
    return f"{_schema}.{table}" if _schema else table


def upgrade() -> None:
    op.create_table(
        "reservation_dynamic_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "reservation_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(f"{_qualified('reservations')}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("template_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=_schema,
    )
    op.create_index(
        "ix_reservation_dynamic_requests_reservation_id",
        "reservation_dynamic_requests",
        ["reservation_id"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservation_dynamic_requests_reservation_id",
        table_name="reservation_dynamic_requests",
        schema=_schema,
    )
    op.drop_table("reservation_dynamic_requests", schema=_schema)
