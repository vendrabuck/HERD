"""Add devices.request_id idempotency key with a unique constraint.

Makes the internal dynamic-instance create idempotent (issue #275, a follow-up
to issue #32 phase 2). Execution redelivers reservation.provision_requested and
re-posts the same booking request_id; the unique constraint plus return-existing
semantics in create_dynamic_instance_device converge on one device row instead
of materializing a second and orphaning the first. The column is nullable and
every admin-created device leaves it NULL; Postgres and SQLite both allow
multiple NULLs in a unique column, so plain devices are unaffected.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-06 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=True),
        schema=_schema,
    )
    op.create_unique_constraint(
        "uq_devices_request_id",
        "devices",
        ["request_id"],
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_constraint("uq_devices_request_id", "devices", type_="unique", schema=_schema)
    op.drop_column("devices", "request_id", schema=_schema)
