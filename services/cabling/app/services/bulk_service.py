"""Bulk import and export of topologies.

A topology export is its canvas (nodes plus edges). Because device references
inside a canvas are raw UUIDs that will not match across instances, export
rewrites each `node.data.device.id` to the device's name (which the canvas
already carries as `node.data.device.name`). Import resolves those names back to
the target instance's device ids over HTTP to the inventory service, creates or
updates the topology by name (a re-imported export updates the original rather
than duplicating it), then runs the existing build_adjacency_graph / validate
path so an imported topology with an unreachable edge is rejected exactly as an
interactively drawn one is.

An update matched by name is guarded like PUT /topologies/{id}: only the
topology's creator or an admin may update it, so a name match on another
user's topology is rejected per-row (issue #464), and a topology held by
another user's active reservation is not rewired, its row is rejected instead
(admins bypass, matching the PUT). When several topologies share a name, the
actor's own is matched before any other user's, so importing your own export
never targets a same-named topology someone else created.

Per-row error handling means one bad topology is rejected with a reason without
aborting the batch. A dry_run import runs full parsing, name resolution, and
validation and returns the per-row report without committing.
"""

import copy
import csv
import io
import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.topology import Topology, TopologyVersion
from app.schemas.bulk import BulkImportReport, RowResult
from app.services.device_resolver import resolve_device_names
from app.services.reservation_guard import find_blocking_reservations
from app.services.version_service import commit_with_new_version

# Pinned reject reason when an import row would rewire a topology currently held
# by another user's active reservation. Mirrors the reservation-scoped lock on
# PUT /topologies/{id}: bulk import must never silently rewire a topology out
# from under a live reservation it does not own.
_LOCKED_TOPOLOGY_REASON = (
    "topology is in use by an active reservation owned by another user; "
    "bulk import cannot rewire it"
)

# Pinned reject reason when an import row matches a topology created by another
# user and the actor is not an admin (issue #464). Mirrors the creator-or-admin
# gate on PUT /topologies/{id}: import is a batched caller of the same edit
# surface, not a back door around its authorization. The "not_authorized"
# prefix is the machine-readable token; the rest explains it to a human.
_NOT_OWNED_TOPOLOGY_REASON = (
    "not_authorized: topology was created by another user; "
    "only its creator or an admin can update it via import"
)

# CSV export of a topology is a flat edge list: one row per canvas edge with the
# endpoint device names. Nodes with no edges are not represented in CSV; CSV is
# a convenience view of the wiring graph, JSON is the lossless round-trip format.
TOPOLOGY_CSV_COLUMNS = [
    "topology_name",
    "source_device",
    "source_port",
    "target_device",
    "target_port",
    "layer",
]


def _empty_report(dry_run: bool) -> BulkImportReport:
    return BulkImportReport(
        dry_run=dry_run, total=0, created=0, updated=0, skipped=0, rejected=0, rows=[]
    )


def _tally(report: BulkImportReport) -> BulkImportReport:
    report.total = len(report.rows)
    report.created = sum(1 for r in report.rows if r.action == "create")
    report.updated = sum(1 for r in report.rows if r.action == "update")
    report.skipped = sum(1 for r in report.rows if r.action == "skip")
    report.rejected = sum(1 for r in report.rows if r.action == "reject")
    return report


# Export ---------------------------------------------------------------------


def _node_device(node: dict[str, Any]) -> dict[str, Any]:
    return (node.get("data") or {}).get("device") or {}


def canvas_ids_to_names(canvas: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of the canvas with each node device id replaced by its name.

    The device id is removed; the name travels under `node.data.device.name`,
    which the editor already populates. Edges reference React Flow node ids (not
    device ids), so they are kept verbatim.
    """
    if not canvas:
        return {"nodes": [], "edges": []}
    out = copy.deepcopy(canvas)
    for node in out.get("nodes") or []:
        device = _node_device(node)
        if "id" in device:
            # Preserve name if present; otherwise the import will reject this
            # node as unresolvable, which is the correct, visible failure.
            device.pop("id", None)
    return out


def topology_to_record(topology: Topology) -> dict[str, Any]:
    return {
        "name": topology.name,
        "canvas": canvas_ids_to_names(topology.canvas_data),
    }


def records_to_json(records: list[dict[str, Any]]) -> str:
    return json.dumps({"resource": "topologies", "version": 1, "items": records}, indent=2)


def topology_to_csv_rows(topology: Topology) -> list[dict[str, Any]]:
    """Flatten a topology's canvas edges into CSV rows keyed by device names."""
    canvas = topology.canvas_data or {}
    nodes = canvas.get("nodes") or []
    edges = canvas.get("edges") or []
    node_to_name: dict[str, str] = {}
    for node in nodes:
        node_id = node.get("id")
        name = _node_device(node).get("name")
        if node_id and name:
            node_to_name[node_id] = name
    rows: list[dict[str, Any]] = []
    for edge in edges:
        edge_data = edge.get("data") or {}
        rows.append(
            {
                "topology_name": topology.name,
                "source_device": node_to_name.get(edge.get("source"), ""),
                "source_port": edge_data.get("sourcePort") or edge.get("sourceHandle") or "",
                "target_device": node_to_name.get(edge.get("target"), ""),
                "target_port": edge_data.get("targetPort") or edge.get("targetHandle") or "",
                "layer": edge_data.get("layer") or "",
            }
        )
    return rows


def records_to_csv(topologies: list[Topology]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=TOPOLOGY_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for topology in topologies:
        for row in topology_to_csv_rows(topology):
            writer.writerow(row)
    return out.getvalue()


# Import parsing -------------------------------------------------------------


def parse_json_topologies(raw: bytes) -> list[dict[str, Any]]:
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if isinstance(doc, dict) and "items" in doc:
        items = doc["items"]
    elif isinstance(doc, list):
        items = doc
    else:
        raise HTTPException(
            status_code=422,
            detail="JSON import must be a list of topologies or an object with an 'items' list",
        )
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="'items' must be a list")
    return items


def parse_csv_topologies(raw: bytes) -> list[dict[str, Any]]:
    """Group flat CSV edge rows into per-topology canvas records.

    Each distinct source/target device name becomes a node; each row becomes an
    edge. Node ids are synthesized deterministically from the device name so an
    edge can reference its endpoints.
    """
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    by_topology: dict[str, dict[str, Any]] = {}
    for row in reader:
        topo_name = (row.get("topology_name") or "").strip()
        if not topo_name:
            continue
        bucket = by_topology.setdefault(topo_name, {"nodes": {}, "edges": []})
        src = (row.get("source_device") or "").strip()
        tgt = (row.get("target_device") or "").strip()
        for dev in (src, tgt):
            if dev and dev not in bucket["nodes"]:
                node_id = f"node-{dev}"
                bucket["nodes"][dev] = {
                    "id": node_id,
                    "data": {"device": {"name": dev}, "label": dev},
                }
        if src and tgt:
            bucket["edges"].append(
                {
                    "id": f"edge-{len(bucket['edges'])}",
                    "source": f"node-{src}",
                    "target": f"node-{tgt}",
                    "data": {
                        "layer": (row.get("layer") or "").strip() or None,
                        "sourcePort": (row.get("source_port") or "").strip() or None,
                        "targetPort": (row.get("target_port") or "").strip() or None,
                    },
                }
            )
    records: list[dict[str, Any]] = []
    for topo_name, bucket in by_topology.items():
        records.append(
            {
                "name": topo_name,
                "canvas": {"nodes": list(bucket["nodes"].values()), "edges": bucket["edges"]},
            }
        )
    return records


def _collect_device_names(canvas: dict[str, Any] | None) -> set[str]:
    names: set[str] = set()
    for node in (canvas or {}).get("nodes") or []:
        name = _node_device(node).get("name")
        if name:
            names.add(name)
    return names


def rewrite_canvas_names_to_ids(
    canvas: dict[str, Any], name_to_id: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Replace each node device name with the resolved local id.

    Returns the rewritten canvas and the list of names that could not be
    resolved (so the caller can reject the topology with a clear reason).
    """
    out = copy.deepcopy(canvas)
    unresolved: list[str] = []
    for node in out.get("nodes") or []:
        device = _node_device(node)
        name = device.get("name")
        if not name:
            continue
        local_id = name_to_id.get(name)
        if local_id is None:
            unresolved.append(name)
            continue
        device["id"] = local_id
    return out, unresolved


# Import ----------------------------------------------------------------------


async def import_topologies(
    db: AsyncSession,
    raw: bytes,
    fmt: str,
    dry_run: bool,
    actor_id: uuid.UUID,
    actor_name: str,
    actor_role: str = "user",
) -> BulkImportReport:
    if fmt == "json":
        records = parse_json_topologies(raw)
    elif fmt == "csv":
        records = parse_csv_topologies(raw)
    else:
        raise HTTPException(status_code=422, detail="format must be 'csv' or 'json'")

    report = _empty_report(dry_run)

    # Resolve every device name across the whole batch in one inventory call,
    # then rewrite per topology. Keeps import O(1) HTTP calls regardless of row
    # count.
    all_names: set[str] = set()
    for rec in records:
        all_names |= _collect_device_names(rec.get("canvas"))
    try:
        name_to_id = await resolve_device_names(sorted(all_names)) if all_names else {}
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"could not resolve device names via inventory: {exc}"
        ) from exc

    # Local import of the validator to avoid a circular import at module load
    # (routes/topologies imports nothing from here, but keep the dependency one-way).
    from app.routes.topologies import _run_topology_validation

    is_admin = actor_role in ("admin", "superadmin")

    for index, rec in enumerate(records):
        name = (rec.get("name") or "").strip()
        try:
            if not name:
                report.rows.append(
                    RowResult(row=index, action="reject", reason="missing required field: name")
                )
                continue

            canvas = rec.get("canvas") or {"nodes": [], "edges": []}
            rewritten, unresolved = rewrite_canvas_names_to_ids(canvas, name_to_id)
            if unresolved:
                report.rows.append(
                    RowResult(
                        row=index,
                        action="reject",
                        identity=name,
                        reason="unresolved device names: " + ", ".join(sorted(set(unresolved))),
                    )
                )
                continue

            # Build a detached Topology to run the existing validator against the
            # rewritten canvas before any write. The validator is read-only on the
            # passed topology and reads connections from the db.
            candidate = Topology(name=name, created_by=actor_id, canvas_data=rewritten)
            validation = await _run_topology_validation(candidate, db)
            if not validation.valid:
                reasons = ", ".join(f"{e.reason}({e.edge_id})" for e in validation.invalid_edges)
                report.rows.append(
                    RowResult(
                        row=index,
                        action="reject",
                        identity=name,
                        reason=f"topology validation failed: {reasons}",
                    )
                )
                continue

            # Match by name to update in place, mirroring the inventory device
            # and template importers so a re-imported export updates the original
            # rather than creating a silent duplicate (issue #336). Topology names
            # carry no unique constraint, so multiple rows may match; the actor's
            # own topology is preferred (issue #464: importing your own export
            # must never target a same-named topology someone else created),
            # falling back to the earliest-created row as the deterministic
            # canonical target.
            matches = list(
                (
                    await db.execute(
                        select(Topology)
                        .where(Topology.name == name)
                        .order_by(Topology.created_at.asc(), Topology.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            existing = next(
                (t for t in matches if str(t.created_by) == str(actor_id)),
                matches[0] if matches else None,
            )

            if existing is None:
                if not dry_run:
                    topology = Topology(
                        name=name,
                        created_by=actor_id,
                        owner_name=actor_name,
                        canvas_data=rewritten,
                    )
                    db.add(topology)
                    await db.flush()
                    snapshot = TopologyVersion(
                        topology_id=topology.id,
                        version_number=1,
                        canvas_data=rewritten,
                        name=name,
                        description="Imported via bulk import",
                        created_by=actor_id,
                        author_name=actor_name,
                    )
                    db.add(snapshot)
                    await db.commit()
                report.rows.append(RowResult(row=index, action="create", identity=name))
                continue

            # Creator-or-admin gate, mirroring PUT /topologies/{id} (issue
            # #464): a non-admin may only update a topology they created. A
            # non-owned name match is rejected per-row with a pinned reason,
            # never silently skipped and never a whole-request 403, keeping
            # the batch semantics of the other reject paths. It runs before
            # the changed-canvas check because the interactive PUT refuses a
            # non-owner regardless of body. The create branch above stays
            # open to any authenticated user, matching POST /topologies.
            if not is_admin and str(existing.created_by) != str(actor_id):
                report.rows.append(
                    RowResult(
                        row=index,
                        action="reject",
                        identity=name,
                        reason=_NOT_OWNED_TOPOLOGY_REASON,
                    )
                )
                continue

            # Update path. Only a canvas that actually differs rewires the
            # topology, so a byte-identical re-import is a no-op update (no new
            # version row, no lock check needed), and the reservation-lock guard
            # only runs when the wiring would change.
            canvas_changed = rewritten != (existing.canvas_data or {"nodes": [], "edges": []})
            if canvas_changed and not is_admin:
                # Reservation-scoped lock, mirroring PUT /topologies/{id}: a
                # topology held by another user's active reservation must not be
                # rewired. The guard fails open if reservations is unreachable.
                blocking = await find_blocking_reservations(existing.id)
                others = [b for b in blocking if str(b.get("user_id") or "") != str(actor_id)]
                if others:
                    report.rows.append(
                        RowResult(
                            row=index,
                            action="reject",
                            identity=name,
                            reason=_LOCKED_TOPOLOGY_REASON,
                        )
                    )
                    continue

            if not dry_run and canvas_changed:
                existing.canvas_data = rewritten
                existing.modified_by = actor_id
                snapshot = TopologyVersion(
                    topology_id=existing.id,
                    canvas_data=rewritten,
                    name=name,
                    description="Updated via bulk import",
                    created_by=actor_id,
                    author_name=actor_name,
                )
                # version_number is allocated as max+1 under the unique
                # constraint; commit_with_new_version serializes concurrent
                # writers rather than risking a raw IntegrityError 500.
                await commit_with_new_version(db, existing, snapshot)
            report.rows.append(RowResult(row=index, action="update", identity=name))
        except HTTPException as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc.detail))
            )
        except Exception as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc))
            )

    return _tally(report)
