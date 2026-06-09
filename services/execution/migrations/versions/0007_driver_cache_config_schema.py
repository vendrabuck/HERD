"""Add config_schema_json column to driver_cache for driver-published schemas.

Captures the JSON-encoded dict returned by Driver.config_schema() once per
SHA256 at first load, paralleling metadata_json (revision 0004). NULL means
the driver did not publish a schema. See issue #23 and
docs/design/0002-driver-published-config-schemas.md.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-09 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "driver_cache",
        sa.Column("config_schema_json", sa.Text(), nullable=True),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("driver_cache", "config_schema_json", schema=_schema)
