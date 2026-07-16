"""Add cancelled_by to reservations.

Issue #340: admins may cancel any reservation. This nullable column records the
acting admin's user_id when the cancel is an admin override (the admin does not
own the reservation). An owner self-cancel leaves it NULL, so a non-null value
is the audit record that an admin force-cancelled the booking on the owner's
behalf. modified_by remains the generic last-writer field.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-16 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("cancelled_by", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservations", "cancelled_by", schema=_schema)
