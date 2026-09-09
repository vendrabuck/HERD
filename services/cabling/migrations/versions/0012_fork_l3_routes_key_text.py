"""Widen fork_l3_routes.route_key from String(400) to Text (issue #758).

`RouteSpec.route_key` (`app/services/l3_intent.py`) JSON-packs all four route
fields; each field may be up to 64 characters, and with the default
`ensure_ascii=True` a non-ASCII field \\uXXXX-escapes to six bytes per
character, so the packed key can exceed 400 characters for input the save gate
otherwise accepts. Postgres then rejects the row inside the locked reconcile
transaction (`StringDataRightTruncation`), which reservations relays as a 503
for a canvas the validator already judged valid. The companion code fix packs
with `ensure_ascii=False`; this migration removes the fixed-width ceiling so
column width can never be the limiting factor again. Postgres btree indexes
cap a key near 2700 bytes; the worst case under the new packing (four 64-char
fields plus JSON quoting/escaping) stays well under that, so the
`(fork_id, device_id, route_key)` unique constraint is unaffected.

Revision 0011 is still unreleased as of this fix (v0.4.0 predates it), so no
stored key changes form on any release; only the gate stack carries 0011.

Revision ID: 0012
Revises: 0011
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def upgrade() -> None:
    op.alter_column(
        "fork_l3_routes",
        "route_key",
        type_=sa.Text(),
        existing_type=sa.String(400),
        existing_nullable=False,
        schema=_schema,
    )


def downgrade() -> None:
    op.alter_column(
        "fork_l3_routes",
        "route_key",
        type_=sa.String(400),
        existing_type=sa.Text(),
        existing_nullable=False,
        schema=_schema,
    )
