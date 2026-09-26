"""Scrub every stored canvas's device nodes down to the allowlist (hardening).

The topology editor used to persist the whole inventory Device record onto each
device node's ``data.device``, including ``field_data``, which can carry
clear-text device credentials. The application-side fix (``strip_device_nodes``
in ``app/services/canvas_nodes.py``) reduces every write to
``DEVICE_NODE_ALLOWED_KEYS`` going forward; this migration is the one-time data
pass that scrubs every row already on disk across the five tables that hold a
canvas: ``topologies``, ``topology_versions``, ``reservation_fork``,
``fork_versions``, and ``topology_templates``.

Batched (never one SELECT for a whole table) and idempotent: a row whose
device nodes are already within the allowlist is left untouched (no UPDATE
issued), so a rerun, or running against a database this fix has already
touched, is a no-op. Every non-device key (nodes with no ``data.device``,
edges, ``viewport``, etc.) is passed through byte-for-byte.

Revision ID: 0013
Revises: 0012
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None

_BATCH_SIZE = 200

# Frozen copy of app.services.canvas_nodes.DEVICE_NODE_ALLOWED_KEYS and
# strip_device_nodes: migrations must not import live application code (a
# later refactor of that module would otherwise break `alembic upgrade` for
# every deployment still below this revision). Kept in sync with the live
# module by review; a unit test
# (tests/test_scrub_device_node_field_data_migration.py) asserts this frozen
# copy's allowlist and behavior match the live module's as of this writing.

DEVICE_NODE_ALLOWED_KEYS = frozenset(
    {
        "id",
        "name",
        "topology_type",
        "connection_type",
        "status",
        "template_name",
        "template_icon",
        "role",
    }
)


def strip_device_nodes(canvas):
    """See app.services.canvas_nodes.strip_device_nodes for the full contract."""
    if not isinstance(canvas, dict):
        return canvas
    nodes = canvas.get("nodes")
    if not isinstance(nodes, list):
        return canvas

    new_nodes = []
    changed = False
    for node in nodes:
        if not isinstance(node, dict):
            new_nodes.append(node)
            continue
        data = node.get("data")
        device = data.get("device") if isinstance(data, dict) else None
        if not isinstance(device, dict):
            new_nodes.append(node)
            continue
        stripped_device = {k: v for k, v in device.items() if k in DEVICE_NODE_ALLOWED_KEYS}
        if stripped_device.keys() == device.keys():
            new_nodes.append(node)
            continue
        changed = True
        new_nodes.append({**node, "data": {**data, "device": stripped_device}})

    if not changed:
        return canvas
    return {**canvas, "nodes": new_nodes}


# (table_name, id_column, canvas_column) for every table that stores a canvas.
_CANVAS_TABLES = [
    ("topologies", "id", "canvas_data"),
    ("topology_versions", "id", "canvas_data"),
    ("reservation_fork", "id", "canvas_data"),
    ("fork_versions", "id", "canvas_data"),
    ("topology_templates", "id", "canvas_data"),
]


def _scrub_table(bind, table_name: str, id_col: str, canvas_col: str) -> None:
    tbl = sa.table(
        table_name,
        sa.column(id_col, sa.Uuid(as_uuid=True)),
        sa.column(canvas_col, sa.JSON()),
        schema=_schema,
    )
    offset = 0
    while True:
        rows = bind.execute(
            sa.select(tbl.c[id_col], tbl.c[canvas_col])
            .order_by(tbl.c[id_col])
            .limit(_BATCH_SIZE)
            .offset(offset)
        ).fetchall()
        if not rows:
            break
        for row_id, canvas in rows:
            if canvas is None:
                continue
            scrubbed = strip_device_nodes(canvas)
            if scrubbed == canvas:
                continue
            bind.execute(
                sa.update(tbl).where(tbl.c[id_col] == row_id).values({canvas_col: scrubbed})
            )
        offset += _BATCH_SIZE


def upgrade() -> None:
    bind = op.get_bind()
    for table_name, id_col, canvas_col in _CANVAS_TABLES:
        _scrub_table(bind, table_name, id_col, canvas_col)


def downgrade() -> None:
    # Data-only migration: the stripped keys (field_data and anything else
    # outside the allowlist) are not recoverable by design, so there is no
    # safe automated down-migration. No-op.
    pass
