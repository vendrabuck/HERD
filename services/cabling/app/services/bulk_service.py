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

Visibility (issue #908): the internal resolve-by-name call to inventory stays
unfiltered (it still answers "does this name exist anywhere", the contract
POST /devices/resolve-by-name has always had); the filtering for a non-admin
caller happens here, in cabling, once per request, via `resolve_caller_visibility`
(issue #763's shared helper: admin means no filter, an unanswerable lookup fails
closed with a 503). A name that resolves to a device outside the caller's
visible set is folded into that row's "unresolved device names" list, the exact
reason and outcome an unresolvable name already produces, so a hidden device and
a nonexistent one are indistinguishable. As a second, independent layer,
`redact_invisible_device_nodes` runs on the rewritten canvas before validation, so
a device reference that bypassed name resolution entirely (a raw `id` carried in
`data.device` with no `name`) still cannot reach the L3 pass or the edge pass as
anything but the existing `missing_device` reason. CSV import shares this same
path: it parses into the identical canvas shape before `import_topologies` runs.
"""

import copy
import csv
import io
import json
import uuid
from typing import Any

from fastapi import HTTPException
from herd_common.csv_safety import csv_safe_cell, csv_unsafe_cell
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.topology import Topology, TopologyVersion
from app.schemas.bulk import BulkImportReport, RowResult
from app.services.canvas_nodes import redact_invisible_device_nodes, strip_device_nodes
from app.services.device_resolver import resolve_device_names
from app.services.reservation_guard import find_blocking_reservations
from app.services.version_service import commit_with_new_version
from app.services.visible_devices import resolve_caller_visibility

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
    # Belt and braces (defense in depth): the stored canvas_data was already
    # reduced to the allowlist when it was written, but export is the one
    # surface that reassembles a response straight from storage for an
    # external file, so it strips again rather than trust that invariant
    # silently.
    return {
        "name": topology.name,
        "canvas": strip_device_nodes(canvas_ids_to_names(topology.canvas_data)),
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
                # Every column here is free text: topology_name is
                # user-writable by any authenticated user (issue #910's most
                # exposed writer), and source/target device and port names
                # come from the stored canvas, not inventory, so they carry
                # no server-side allowlist either. layer is a canvas
                # annotation only (ADR 0009 option C), never validated
                # against a fixed enum, so it is neutralized too rather than
                # treated as a safe enumeration.
                "topology_name": csv_safe_cell(topology.name),
                "source_device": csv_safe_cell(node_to_name.get(edge.get("source"), "")),
                "source_port": csv_safe_cell(
                    edge_data.get("sourcePort") or edge.get("sourceHandle") or ""
                ),
                "target_device": csv_safe_cell(node_to_name.get(edge.get("target"), "")),
                "target_port": csv_safe_cell(
                    edge_data.get("targetPort") or edge.get("targetHandle") or ""
                ),
                "layer": csv_safe_cell(edge_data.get("layer") or ""),
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
        # Every cell here may carry a csv_safe_cell neutralization quote
        # added by records_to_csv (issue #910); csv_unsafe_cell strips it
        # back off before the value is stripped/used, so an exported name
        # like "=1+1" round-trips through import to the exact original
        # rather than staying quoted forever.
        topo_name = (csv_unsafe_cell(row.get("topology_name")) or "").strip()
        if not topo_name:
            continue
        bucket = by_topology.setdefault(topo_name, {"nodes": {}, "edges": []})
        src = (csv_unsafe_cell(row.get("source_device")) or "").strip()
        tgt = (csv_unsafe_cell(row.get("target_device")) or "").strip()
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
                        "layer": (csv_unsafe_cell(row.get("layer")) or "").strip() or None,
                        "sourcePort": (csv_unsafe_cell(row.get("source_port")) or "").strip()
                        or None,
                        "targetPort": (csv_unsafe_cell(row.get("target_port")) or "").strip()
                        or None,
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


def _hidden_names_in_canvas(
    canvas: dict[str, Any] | None,
    name_to_id: dict[str, str],
    visible_ids: set[uuid.UUID],
) -> set[str]:
    """Names in ``canvas`` that resolved to a real device id outside ``visible_ids``.

    Issue #908: a non-admin caller must not learn, via the per-row reject
    reason, that a device name exists but is outside their device-group
    visibility. Every such name is returned here so the caller can fold it
    into the row's "unresolved device names" set, making a hidden name and a
    nonexistent one produce an identical per-row report.
    """
    hidden: set[str] = set()
    for name in _collect_device_names(canvas):
        local_id = name_to_id.get(name)
        if local_id is None:
            continue
        try:
            device_uuid = uuid.UUID(local_id)
        except (ValueError, TypeError):
            continue
        if device_uuid not in visible_ids:
            hidden.add(name)
    return hidden


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
    authorization: str | None = None,
) -> BulkImportReport:
    if fmt == "json":
        records = parse_json_topologies(raw)
    elif fmt == "csv":
        records = parse_csv_topologies(raw)
    else:
        raise HTTPException(status_code=422, detail="format must be 'csv' or 'json'")

    report = _empty_report(dry_run)

    # Issue #908: resolve the caller's device visibility ONCE per request,
    # before any name is resolved or any row is judged, mirroring the #763
    # pattern on POST /topologies/{id}/validate. None for an admin (no
    # filter, no inventory call); a non-admin's own visible-device set
    # otherwise. An unanswerable lookup fails CLOSED with a 503, never an
    # unfiltered pass.
    visible_ids = await resolve_caller_visibility(
        {"sub": str(actor_id), "role": actor_role},
        authorization,
        unavailable_detail=(
            "Could not verify device visibility; the import was not processed. Retry the request."
        ),
    )

    # Resolve every device name across the whole batch in one inventory call,
    # then rewrite per topology. Keeps import O(1) HTTP calls regardless of row
    # count. This lookup stays unfiltered by design (issue #908): it answers
    # whether a name exists anywhere, exactly the internal resolve-by-name
    # contract already had; the visibility filter above is applied per row,
    # below, so a hidden device produces the same outcome as a nonexistent one.
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
    from app.services.topology_validation import run_full_topology_validation

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
            if visible_ids is not None:
                # Issue #908: a name that DID resolve, but to a device outside
                # this non-admin caller's visibility, is folded into the same
                # unresolved set, so the reject reason below is byte-identical
                # to a genuinely nonexistent name.
                unresolved = sorted(
                    set(unresolved) | _hidden_names_in_canvas(canvas, name_to_id, visible_ids)
                )
            # An imported file is caller-supplied input like any other: reduce
            # every device node's `data.device` to the allowlist before it is
            # validated or written.
            rewritten = strip_device_nodes(rewritten)
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

            if visible_ids is not None:
                # Issue #908, second layer (defense in depth): a device node
                # can carry a raw `data.device.id` with no `name` at all,
                # bypassing name resolution (and the fold above) entirely.
                # Redact any such node exactly as the user-facing validate
                # route does (issue #763), so it reports the existing
                # `missing_device` reason and never reaches the L3 pass.
                rewritten = redact_invisible_device_nodes(rewritten, visible_ids)

            # Run the existing full validator (edges + L3) against the rewritten
            # canvas before any write; it is read-only, reading connections (and,
            # for a canvas carrying data.l3, inventory) but never touching this row.
            validation = await run_full_topology_validation(rewritten, db)
            if not validation.valid:
                reasons = [f"{e.reason}({e.edge_id})" for e in validation.invalid_edges]
                # S9 review fix, round 2: route reasons ride the same per-row
                # reject message as edge reasons, "<node_id>[<index>] (<reason>)",
                # so a routes-only (or combined) failure names what's actually
                # wrong instead of an empty edges-only reason string.
                reasons += [
                    f"{r.node_id}[{r.index if r.index is not None else '-'}] ({r.reason})"
                    for r in validation.invalid_routes
                ]
                report.rows.append(
                    RowResult(
                        row=index,
                        action="reject",
                        identity=name,
                        reason=f"topology validation failed: {', '.join(reasons)}",
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
            # S9 review fix, round 2: a 503 (l3_config_unavailable: inventory
            # could not be asked to judge this row's routing intent at all)
            # STOPS the import request from processing any further row, the
            # same way the upfront resolve_device_names failure does, rather
            # than being swallowed as a per-row reject: unlike a genuine
            # validation failure, this row was never actually judged, so
            # "reject this one row" would be misleading and the rest of the
            # batch is equally unjudged. This does NOT roll back rows already
            # processed: each prior row committed its own write individually
            # (see the per-row `await db.commit()` above), so any row before
            # this one that created or updated a topology keeps that write;
            # only this row's own uncommitted change is rolled back and no
            # further row is attempted.
            if exc.status_code == 503:
                raise
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
