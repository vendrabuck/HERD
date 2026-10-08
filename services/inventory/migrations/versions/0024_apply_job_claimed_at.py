"""Add device_config_apply_jobs.claimed_at: when a scheduler claimed the job.

Issue #1089. The stale-running sweep returned a running job to pending when its
scheduled_for was more than five minutes old, so a job claimed late (after an
outage or behind a backlog) could be re-queued while its claimer was still firing
it, and a second scheduler would fire it again. The claim now writes claimed_at and
the sweep measures from it. Existing rows get null, which the sweep treats as the
old rule (it falls back to scheduled_for).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-08 00:00:01.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "device_config_apply_jobs",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("device_config_apply_jobs", "claimed_at", schema=_schema)
