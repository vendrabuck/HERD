"""Add reservations.purpose_suggestion and its supporting columns.

Issue #646 phase 2 (lab purpose classification, decisions recorded for ADR
0013 points 8-11). purpose_suggestion stores the AI orchestrator's
classify-purpose response verbatim (a JSON object); none_as_null=True keeps a
Python None mapped to SQL NULL rather than a JSON "null" literal, which is
load-bearing for every IS NULL/IS NOT NULL filter this phase adds (the sweep
reconciler's eligibility check, the admin review list, and backfill).
purpose_classify_requested_at is stamped once at the five reservation-lifecycle
sites that transition a reservation into COMPLETED/CANCELLED/FAILED, and is
the only way a row becomes eligible for background classification.
purpose_classify_attempts caps sweep retries. purpose_suggestion_dismissed_at
marks a suggestion an admin reviewed and declined.

Column-add plus one index, so the create_all-vs-migration hazard (issue #419)
does not apply: schema_init's create_all never touches an existing table.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-04 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("purpose_suggestion", sa.JSON(none_as_null=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column("purpose_suggested_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column("purpose_classify_requested_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column(
            "purpose_classify_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column("purpose_suggestion_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_reservations_purpose_classify_requested_at",
        "reservations",
        ["purpose_classify_requested_at"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservations_purpose_classify_requested_at",
        table_name="reservations",
        schema=_schema,
    )
    op.drop_column("reservations", "purpose_suggestion_dismissed_at", schema=_schema)
    op.drop_column("reservations", "purpose_classify_attempts", schema=_schema)
    op.drop_column("reservations", "purpose_classify_requested_at", schema=_schema)
    op.drop_column("reservations", "purpose_suggested_at", schema=_schema)
    op.drop_column("reservations", "purpose_suggestion", schema=_schema)
