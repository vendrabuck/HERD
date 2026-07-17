"""Create l1_connection_assignments and reservation_wiring_state.

Connection-driven reconcile scaffold (ADR 0007, issue #345 P3b phase 1).

l1_connection_assignments is the connection-addressable applied L1 state that
replaces the execution_runs inference at
execution_service.action_succeeded_for_reservation. The partial-unique index
allows at most one ACTIVE cross-connect per switch port pair; RELEASED and FAILED
rows are retained and never block a new claim, so a moved cable's
release-before-build never self-collides.

reservation_wiring_state is the per-reservation ordering and teardown anchor;
phase 1 only creates it (and sets frozen on terminal events at runtime), leaving
last_applied_fork_version stamping for phase 3.

Backfill (ADR 0007 Decision 4): reservations already ACTIVE at upgrade have live
cross-connects but no rows. This revision reconstructs ACTIVE rows from the
existing SUCCESS connect_ports execution_runs, the same inference
action_succeeded_for_reservation performs, refined to current state (a pair with
a later SUCCESS disconnect_ports is not live). physical_connection_id and
reservation_wiring_state.last_applied_fork_version are left NULL: neither is
reconstructable from execution_runs (physical_connection_id was never recorded on
a run, and the pre-P3b fork-version baseline lives in cabling). The step is safe
on an empty database and idempotent per row (an existing ACTIVE pair is skipped).

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-17 00:00:00.000000
"""

import os
import uuid

import sqlalchemy as sa
from alembic import op


# Frozen copy of the backfill logic from app/services/l1_assignment_service.py at
# the time of this revision. Migrations are historical snapshots and must not
# import live application code: a later rename or refactor of the service module
# would break `alembic upgrade` for deployments still below this revision. The
# live module keeps its own copy for unit tests and runtime reuse; the two are
# intentionally duplicated, not shared.
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


def compute_backfill_assignments(runs):
    """Reconstruct currently-live L1 cross-connects from execution_run rows.

    A pair is live only when its latest SUCCESS connect_ports is not followed by
    a SUCCESS disconnect_ports at or after it. Keyed on (switch, canonical pair)
    so at most one ACTIVE row per pair is emitted; physical_connection_id is not
    reconstructable from execution_runs and is inserted as null by the caller.
    """
    latest_connect = {}
    latest_disconnect = {}
    for r in runs:
        if _row_get(r, "status") != "SUCCESS":
            continue
        action = _row_get(r, "action")
        if action not in ("connect_ports", "disconnect_ports"):
            continue
        switch_id = _row_get(r, "device_id")
        port_a = _row_get(r, "port_a")
        port_b = _row_get(r, "port_b")
        if switch_id is None or port_a is None or port_b is None:
            continue
        ca, cb = _canonical_port_pair(port_a, port_b)
        key = (switch_id, ca, cb)
        created = _row_get(r, "created_at")
        if action == "connect_ports":
            prev = latest_connect.get(key)
            if prev is None or _strictly_after(created, prev[0]):
                latest_connect[key] = (created, _row_get(r, "reservation_id"))
        else:
            prev = latest_disconnect.get(key)
            if prev is None or _strictly_after(created, prev):
                latest_disconnect[key] = created
    result = []
    for key, (c_created, reservation_id) in latest_connect.items():
        if reservation_id is None:
            continue
        d_created = latest_disconnect.get(key)
        if d_created is not None and not _strictly_after(c_created, d_created):
            continue
        switch_id, ca, cb = key
        result.append(
            {
                "reservation_id": reservation_id,
                "switch_device_id": switch_id,
                "port_a": ca,
                "port_b": cb,
            }
        )
    return result


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


def _backfill_active_assignments() -> None:
    """Reconstruct ACTIVE L1 assignment rows from SUCCESS connect_ports runs."""
    bind = op.get_bind()

    runs_tbl = sa.table(
        "execution_runs",
        sa.column("reservation_id"),
        sa.column("device_id"),
        sa.column("action"),
        sa.column("status"),
        sa.column("port_a"),
        sa.column("port_b"),
        sa.column("created_at"),
        schema=_schema,
    )
    l1_tbl = sa.table(
        "l1_connection_assignments",
        sa.column("id"),
        sa.column("reservation_id"),
        sa.column("switch_device_id"),
        sa.column("port_a"),
        sa.column("port_b"),
        sa.column("physical_connection_id"),
        sa.column("status"),
        sa.column("attempts"),
        sa.column("released_at"),
        schema=_schema,
    )

    runs = bind.execute(
        sa.select(
            runs_tbl.c.reservation_id,
            runs_tbl.c.device_id,
            runs_tbl.c.action,
            runs_tbl.c.status,
            runs_tbl.c.port_a,
            runs_tbl.c.port_b,
            runs_tbl.c.created_at,
        ).where(
            runs_tbl.c.action.in_(["connect_ports", "disconnect_ports"]),
            runs_tbl.c.status == "SUCCESS",
        )
    ).fetchall()

    desired = compute_backfill_assignments(runs)
    if not desired:
        return

    # Idempotency: skip pairs already ACTIVE (a rerun of this data step).
    existing_rows = bind.execute(
        sa.select(
            l1_tbl.c.switch_device_id,
            l1_tbl.c.port_a,
            l1_tbl.c.port_b,
        ).where(l1_tbl.c.status == "ACTIVE")
    ).fetchall()
    existing = {(str(r.switch_device_id), r.port_a, r.port_b) for r in existing_rows}

    to_insert = []
    for row in desired:
        key = (str(row["switch_device_id"]), row["port_a"], row["port_b"])
        if key in existing:
            continue
        existing.add(key)
        to_insert.append(
            {
                "id": uuid.uuid4(),
                "reservation_id": row["reservation_id"],
                "switch_device_id": row["switch_device_id"],
                "port_a": row["port_a"],
                "port_b": row["port_b"],
                "physical_connection_id": None,
                "status": "ACTIVE",
                "attempts": 0,
                "released_at": None,
            }
        )

    if to_insert:
        bind.execute(sa.insert(l1_tbl), to_insert)


def upgrade() -> None:
    op.create_table(
        "l1_connection_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("switch_device_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("port_a", sa.String(128), nullable=False),
        sa.Column("port_b", sa.String(128), nullable=False),
        sa.Column("physical_connection_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema,
    )

    op.create_index(
        "uq_l1_active_per_switch_pair",
        "l1_connection_assignments",
        ["switch_device_id", "port_a", "port_b"],
        unique=True,
        schema=_schema,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "reservation_wiring_state",
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("last_applied_fork_version", sa.Integer(), nullable=True),
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=_schema,
    )

    _backfill_active_assignments()


def downgrade() -> None:
    op.drop_table("reservation_wiring_state", schema=_schema)
    op.drop_index(
        "uq_l1_active_per_switch_pair",
        table_name="l1_connection_assignments",
        schema=_schema,
    )
    op.drop_table("l1_connection_assignments", schema=_schema)
