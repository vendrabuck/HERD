"""Add supports_vrf column to driver_packages.

ADR 0014 addendum X-G (issue #755). A Layer 3 driver package declares VRF
support via `driver_metadata.json` at its package root with
`{"supports_vrf": true}`, parallel to `supports_dry_run` (revision 0016). The
inventory service parses it on upload and persists it here so the flag is
readable without re-extracting the package.

Opt-in and closed by default (server_default false), which is also the correct
value for every package uploaded before this revision: a driver that never
declared VRF support must never be handed a `virtual_router` keyword, since
every shipped L3 signature ends in `**_` and would swallow it in silence.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-12 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.add_column(
        "driver_packages",
        sa.Column(
            "supports_vrf",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema=_schema,
    )


def downgrade() -> None:
    op.drop_column("driver_packages", "supports_vrf", schema=_schema)
