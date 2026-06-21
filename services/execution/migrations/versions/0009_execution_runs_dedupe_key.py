"""Add execution_runs.dedupe_key for NATS-redelivery idempotency.

The execution consumer re-ran driver actions (connect_ports, create_vlan,
add_to_vlan, ...) on every NAK redelivery because nothing keyed an action to
its source message. dedupe_key stores the NATS "<stream>:<sequence>" of the
reservation event, stable across redeliveries, so the consumer can skip an
action whose SUCCESS run already carries the same key (issue #133).

Adds:
- execution_runs.dedupe_key (nullable String(128)).
- ix_execution_runs_dedupe_device_action: serves the guard lookup
  "SUCCESS run for this (dedupe_key, device_id, action)?" as an index scan.

Nullable and unconstrained: existing rows and non-event runs (health
scheduler) keep dedupe_key NULL, and a NULL key never matches the guard, so
those paths are unaffected.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-21 00:00:00.000000
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
        "execution_runs",
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_execution_runs_dedupe_device_action",
        "execution_runs",
        ["dedupe_key", "device_id", "action"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_runs_dedupe_device_action",
        table_name="execution_runs",
        schema=_schema,
    )
    op.drop_column("execution_runs", "dedupe_key", schema=_schema)
