"""Add reservations.purpose_category, purpose_category_set_by, purpose_category_set_at.

Issue #646 phase 1 (lab purpose classification, decisions recorded for ADR 0013).
purpose_category is a plain nullable string validated against the reservations
service's configured taxonomy at write time, not a Postgres enum and not a
categories table, so a row keeps its value even if that value later drops out of
the configured list. purpose_category_set_by/purpose_category_set_at record who
classified the reservation and when; the two are always written or cleared
together with the category itself.

Column-add plus an index, so the create_all-vs-migration hazard (issue #419)
does not apply: schema_init's create_all never touches an existing table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-04 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("purpose_category", sa.Text(), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column("purpose_category_set_by", sa.Uuid(), nullable=True),
        schema=_schema,
    )
    op.add_column(
        "reservations",
        sa.Column("purpose_category_set_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )
    op.create_index(
        "ix_reservations_purpose_category",
        "reservations",
        ["purpose_category"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_index("ix_reservations_purpose_category", table_name="reservations", schema=_schema)
    op.drop_column("reservations", "purpose_category_set_at", schema=_schema)
    op.drop_column("reservations", "purpose_category_set_by", schema=_schema)
    op.drop_column("reservations", "purpose_category", schema=_schema)
