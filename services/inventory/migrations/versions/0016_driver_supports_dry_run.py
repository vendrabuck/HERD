"""Add supports_dry_run column to driver_packages.

Driver packages declare dry-run capability via a `driver_metadata.json` file
at their package root with `{"supports_dry_run": true}`. The inventory
service parses this on upload and persists the flag here so the schedule
endpoint can gate dry-run apply jobs without re-extracting the package.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-26 00:00:00.000000
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
        "driver_packages",
        sa.Column(
            "supports_dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("driver_packages", "supports_dry_run", schema=_schema)
