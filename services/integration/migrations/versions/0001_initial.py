"""Initial empty migration: create the integration schema.

The v1 facade is stateless, so this revision defines no tables. It exists to stand
up the service's DB infrastructure (the `integration` schema and the alembic
version table) so phase 4 (webhooks) can add tables cleanly on a clean chain.

Revision ID: 0001
Revises:
Create Date: 2026-06-29 00:00:00.000000
"""

import os

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    if _schema:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema}")


def downgrade() -> None:
    pass
