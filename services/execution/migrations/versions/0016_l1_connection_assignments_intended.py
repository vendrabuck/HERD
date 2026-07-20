"""Add intended to l1_connection_assignments and backfill it.

ADR 0009 Decision 2 (docs/design/0009-l2-l3-connection-driven-reconcile.md), issue
#369: FAILED rows are retried in the wrong direction because nothing on the row
records which way the failed write was going. `intended` (ACTIVE/RELEASED) closes
that gap: a build failure records ACTIVE, a release failure records RELEASED, and
both retry channels split on it instead of always reattempting connect_ports.

Backfill for rows that predate the column:

  - ACTIVE and RELEASED rows: intended = status. These are unambiguous; the row's
    own status already IS its most recent confirmed direction.
  - FAILED rows: reconstructed from the most recent connect_ports/disconnect_ports
    execution_run for the same (reservation, switch device, canonical port pair),
    REGARDLESS of that run's own SUCCESS/FAILED status (unlike migration 0014's
    backfill, which only considers SUCCESS runs to infer CURRENT live state; here
    we want the last ATTEMPTED direction, and the FAILED row exists precisely
    because that attempt did not succeed). connect_ports means intended ACTIVE,
    disconnect_ports means intended RELEASED. A FAILED row with no matching run
    (e.g. an unresolvable-hop park that never reached a driver call) defaults to
    ACTIVE, matching the column's server_default.

The step is idempotent (a rerun recomputes the same values) and safe on an empty
database.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-19 00:00:00.000000
"""

import os

import sqlalchemy as sa
from alembic import op

# Frozen copies of the reconstruction helpers, mirroring migration 0014's discipline:
# migrations are historical snapshots and must not import live application code, since
# a later rename/refactor of the service module would break `alembic upgrade` for
# deployments still below this revision. The live module (l1_assignment_service.py)
# keeps its own copy for unit tests and runtime reuse.


def _canonical_port_pair(port_a, port_b):
    a, b = str(port_a), str(port_b)
    return (a, b) if a <= b else (b, a)


def _row_get(row, name):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _strictly_after(a, b):
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def compute_intended_for_failed_rows(failed_rows, runs):
    """Map each FAILED row's id to its reconstructed intended direction.

    `failed_rows` exposes id, reservation_id, switch_device_id, port_a, port_b
    (already canonical). `runs` exposes reservation_id, device_id (the switch),
    action, port_a, port_b, created_at; every connect_ports/disconnect_ports run
    is considered regardless of its own status. Returns {row_id: "ACTIVE"|"RELEASED"}.
    """
    latest: dict[tuple, tuple] = {}
    for r in runs:
        action = _row_get(r, "action")
        if action not in ("connect_ports", "disconnect_ports"):
            continue
        reservation_id = _row_get(r, "reservation_id")
        switch_id = _row_get(r, "device_id")
        port_a = _row_get(r, "port_a")
        port_b = _row_get(r, "port_b")
        if reservation_id is None or switch_id is None or port_a is None or port_b is None:
            continue
        ca, cb = _canonical_port_pair(port_a, port_b)
        key = (reservation_id, switch_id, ca, cb)
        created = _row_get(r, "created_at")
        prev = latest.get(key)
        if prev is None or _strictly_after(created, prev[0]):
            latest[key] = (created, action)

    result: dict = {}
    for row in failed_rows:
        row_id = _row_get(row, "id")
        key = (
            _row_get(row, "reservation_id"),
            _row_get(row, "switch_device_id"),
            _row_get(row, "port_a"),
            _row_get(row, "port_b"),
        )
        match = latest.get(key)
        if match is None:
            result[row_id] = "ACTIVE"
        else:
            result[row_id] = "ACTIVE" if match[1] == "connect_ports" else "RELEASED"
    return result


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _backfill_intended() -> None:
    bind = op.get_bind()

    l1_tbl = sa.table(
        "l1_connection_assignments",
        sa.column("id"),
        sa.column("reservation_id"),
        sa.column("switch_device_id"),
        sa.column("port_a"),
        sa.column("port_b"),
        sa.column("status"),
        sa.column("intended"),
        schema=_schema,
    )
    runs_tbl = sa.table(
        "execution_runs",
        sa.column("reservation_id"),
        sa.column("device_id"),
        sa.column("action"),
        sa.column("port_a"),
        sa.column("port_b"),
        sa.column("created_at"),
        schema=_schema,
    )

    # ACTIVE and RELEASED rows: intended = status (unambiguous).
    bind.execute(
        sa.update(l1_tbl)
        .where(l1_tbl.c.status.in_(["ACTIVE", "RELEASED"]))
        .values(intended=l1_tbl.c.status)
    )

    failed_rows = bind.execute(
        sa.select(
            l1_tbl.c.id,
            l1_tbl.c.reservation_id,
            l1_tbl.c.switch_device_id,
            l1_tbl.c.port_a,
            l1_tbl.c.port_b,
        ).where(l1_tbl.c.status == "FAILED")
    ).fetchall()
    if not failed_rows:
        return

    runs = bind.execute(
        sa.select(
            runs_tbl.c.reservation_id,
            runs_tbl.c.device_id,
            runs_tbl.c.action,
            runs_tbl.c.port_a,
            runs_tbl.c.port_b,
            runs_tbl.c.created_at,
        ).where(runs_tbl.c.action.in_(["connect_ports", "disconnect_ports"]))
    ).fetchall()

    intended_by_id = compute_intended_for_failed_rows(failed_rows, runs)
    for row_id, intended in intended_by_id.items():
        bind.execute(sa.update(l1_tbl).where(l1_tbl.c.id == row_id).values(intended=intended))


def upgrade() -> None:
    op.add_column(
        "l1_connection_assignments",
        sa.Column("intended", sa.String(20), nullable=False, server_default="ACTIVE"),
        schema=_schema,
    )
    _backfill_intended()


def downgrade() -> None:
    op.drop_column("l1_connection_assignments", "intended", schema=_schema)
