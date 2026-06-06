"""Add expiry_reminder_sent_at to reservations.

ROADMAP #40: the expiration task emits a reservation.expiring_soon reminder onto
HERD_RESERVATIONS when an ACTIVE reservation's end_time falls within a
configurable lead window. This nullable timestamp records when the reminder was
sent so it fires exactly once per reservation across expiration ticks. Null
means "not yet reminded".

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-06 00:00:00.000000
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
    op.add_column(
        "reservations",
        sa.Column("expiry_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservations", "expiry_reminder_sent_at", schema=_schema)
