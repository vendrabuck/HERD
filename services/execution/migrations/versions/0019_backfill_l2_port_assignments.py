"""Backfill l2_port_assignments from historical add_to_vlan runs.

ADR 0009 phase 4 (docs/design/0009-l2-l3-connection-driven-reconcile.md), issue #416.
Data-only: this migration creates NO table (migration 0017 created
l2_port_assignments), so it needs no sa.inspect(...).has_table guard (the #419
create_all-vs-migration hazard applies only to bare op.create_table on a table that
create_all may have already built). It only inserts rows.

Pre-phase-4 reservations have their L2 memberships in execution_runs (SUCCESS
add_to_vlan) but not in the new ledger, so the first connection-driven reconcile
after upgrade would see an empty membership set and re-add every port the legacy path
already applied. This backfill reconstructs the currently-live memberships (an
add_to_vlan not followed by a remove_from_vlan) and records them ACTIVE, keyed to the
reservation's ACTIVE vlan_assignment for the switch's fabric, so the first heal is a
no-op for already-applied ports. It is idempotent (a rerun skips rows already present
by the ACTIVE (switch, port, vlan_assignment_id) key) and tolerates absent or malformed
run/allocation payloads by skipping them.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-20 00:00:00.000000
"""

import os
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_schema = os.environ.get("DB_SCHEMA") or None


# Frozen copy of l2_membership_service.compute_backfill_l2_memberships: migrations must
# not import live application code (a later refactor would break `alembic upgrade` for
# deployments below this revision). Kept in sync with the live module by review.


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


def compute_backfill_l2_memberships(runs, vlan_assignments):
    """Reconstruct currently-live L2 memberships. See the live module for the contract."""
    latest_add: dict = {}
    latest_remove: dict = {}

    for r in runs:
        if _row_get(r, "status") != "SUCCESS":
            continue
        action = _row_get(r, "action")
        if action not in ("add_to_vlan", "remove_from_vlan"):
            continue
        reservation_id = _row_get(r, "reservation_id")
        switch_id = _row_get(r, "device_id")
        port = _row_get(r, "port_a")
        if reservation_id is None or switch_id is None or port is None:
            continue
        key = (reservation_id, switch_id, str(port))
        created = _row_get(r, "created_at")
        target = latest_add if action == "add_to_vlan" else latest_remove
        prev = target.get(key)
        if prev is None or _strictly_after(created, prev):
            target[key] = created

    alloc_by_res_switch: dict = {}
    for va in vlan_assignments:
        if _row_get(va, "status") != "ACTIVE":
            continue
        res_id = _row_get(va, "reservation_id")
        va_id = _row_get(va, "id")
        sids = _row_get(va, "switch_device_ids") or []
        for sid in sids:
            alloc_by_res_switch[(res_id, str(sid))] = va_id

    result = []
    for key, add_created in latest_add.items():
        remove_created = latest_remove.get(key)
        if remove_created is not None and not _strictly_after(add_created, remove_created):
            continue
        reservation_id, switch_id, port = key
        va_id = alloc_by_res_switch.get((reservation_id, str(switch_id)))
        if va_id is None:
            continue
        result.append(
            {
                "reservation_id": reservation_id,
                "vlan_assignment_id": va_id,
                "switch_device_id": switch_id,
                "port": port,
            }
        )
    return result


def upgrade() -> None:
    bind = op.get_bind()

    l2_tbl = sa.table(
        "l2_port_assignments",
        sa.column("id"),
        sa.column("reservation_id"),
        sa.column("vlan_assignment_id"),
        sa.column("switch_device_id"),
        sa.column("port"),
        sa.column("intended"),
        sa.column("status"),
        sa.column("attempts"),
        sa.column("created_at"),
        schema=_schema,
    )
    runs_tbl = sa.table(
        "execution_runs",
        sa.column("reservation_id"),
        sa.column("device_id"),
        sa.column("action"),
        sa.column("status"),
        sa.column("port_a"),
        sa.column("created_at"),
        schema=_schema,
    )
    vlan_tbl = sa.table(
        "vlan_assignments",
        sa.column("id"),
        sa.column("reservation_id"),
        sa.column("switch_device_ids"),
        sa.column("status"),
        schema=_schema,
    )

    runs = bind.execute(
        sa.select(
            runs_tbl.c.reservation_id,
            runs_tbl.c.device_id,
            runs_tbl.c.action,
            runs_tbl.c.status,
            runs_tbl.c.port_a,
            runs_tbl.c.created_at,
        ).where(runs_tbl.c.action.in_(["add_to_vlan", "remove_from_vlan"]))
    ).fetchall()
    if not runs:
        return

    vlan_assignments = bind.execute(
        sa.select(
            vlan_tbl.c.id,
            vlan_tbl.c.reservation_id,
            vlan_tbl.c.switch_device_ids,
            vlan_tbl.c.status,
        ).where(vlan_tbl.c.status == "ACTIVE")
    ).fetchall()

    memberships = compute_backfill_l2_memberships(runs, vlan_assignments)
    if not memberships:
        return

    # Existing ACTIVE membership keys, so a rerun (or a stack that already has ledger
    # rows) never duplicates or trips the ACTIVE (switch, port, vlan) unique index.
    existing = bind.execute(
        sa.select(
            l2_tbl.c.switch_device_id,
            l2_tbl.c.port,
            l2_tbl.c.vlan_assignment_id,
        ).where(l2_tbl.c.status == "ACTIVE")
    ).fetchall()
    existing_keys = {(str(r[0]), str(r[1]), str(r[2])) for r in existing}

    now = datetime.now(timezone.utc)
    for m in memberships:
        key = (str(m["switch_device_id"]), str(m["port"]), str(m["vlan_assignment_id"]))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        bind.execute(
            sa.insert(l2_tbl).values(
                id=uuid.uuid4(),
                reservation_id=m["reservation_id"],
                vlan_assignment_id=m["vlan_assignment_id"],
                switch_device_id=m["switch_device_id"],
                port=str(m["port"]),
                intended="ACTIVE",
                status="ACTIVE",
                attempts=0,
                created_at=now,
            )
        )


def downgrade() -> None:
    # Data-only migration: the rows it inserted are indistinguishable from
    # reconcile-written ACTIVE rows, so there is no safe automated down-migration.
    # Dropping the table is migration 0017's downgrade; this is a no-op.
    pass
