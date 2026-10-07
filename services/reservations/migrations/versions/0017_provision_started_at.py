"""Add reservations.provision_started_at.

Issue #997: the expiration sweep's two PENDING_PROVISION backstops (the dynamic
timeout and the physical-only restart revert) measured provision_timeout_seconds
from updated_at, which any write to the row moves (a purpose-category PATCH is
allowed in every status), so such a write restarted the timeout. This column
records when the row last ENTERED PENDING_PROVISION; it is written in the same
statement as every transition into that status (the create path's insert and the
sweep's claim) and is not cleared on leaving, since only PENDING_PROVISION rows
read it. Rows that predate this revision keep NULL, and the backstops fall back
to updated_at for them.

A nullable column add, so the create_all-vs-migration hazard (issue #419) does
not apply.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("provision_started_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("reservations", "provision_started_at", schema=_schema)
