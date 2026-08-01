"""Bulk import and export endpoints for topologies.

Export streams every topology as CSV (a flat edge list) or JSON (the full canvas
with device references carried by name). Import accepts an uploaded file,
resolves device names to local ids via the inventory service, creates or updates
each topology by name (a re-imported export updates the original rather than
duplicating it), and runs the existing validator so an unreachable edge is
rejected. A dry_run import writes nothing and returns the per-row report.

RBAC matches the interactive topology surface: creating topologies is open to any
authenticated user, so import is too; export likewise. The per-topology
creator-or-admin gate that governs PUT /topologies/{id} is enforced per row on
the import update path (issue #464): a row matching a topology created by
another user is rejected, not silently skipped, unless the actor is an admin.
"""

import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_payload
from app.models.topology import Topology
from app.schemas.bulk import BulkImportReport
from app.services.bulk_service import (
    import_topologies,
    records_to_csv,
    records_to_json,
    topology_to_record,
)

router = APIRouter(prefix="/topologies", tags=["topology-bulk"])


@router.get("/export")
async def export_topologies(
    format: str = Query("json", pattern="^(csv|json)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Export every topology to CSV or JSON.

    JSON is the lossless round-trip format (full canvas, device references by
    name). CSV is a flat edge list, convenient for spreadsheets but it does not
    carry isolated nodes.
    """
    topologies = list((await db.execute(select(Topology).order_by(Topology.name))).scalars().all())
    if format == "csv":
        body = records_to_csv(topologies)
        media, ext = "text/csv", "csv"
    else:
        body = records_to_json([topology_to_record(t) for t in topologies])
        media, ext = "application/json", "json"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="topologies.{ext}"'},
    )


@router.post("/import", response_model=BulkImportReport)
async def import_topologies_endpoint(
    file: UploadFile = File(...),
    format: str = Query("json", pattern="^(csv|json)$"),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    """Import topologies from CSV or JSON.

    Each topology is matched by name: an existing topology is updated in place
    (appending a new version), otherwise a new one is created. When several
    topologies share the name, the caller's own is matched before any other
    user's. Updating is creator-or-admin, enforced per row: a name match on a
    topology created by another user is rejected with a pinned not_authorized
    reason (admins bypass, matching the interactive PUT). Each is validated
    through the existing build_adjacency_graph / validate path before any write;
    one with an unreachable edge is rejected. A topology held by another user's
    active reservation is not silently rewired: a would-be update to its wiring
    is rejected per-row (admins bypass this, matching the interactive PUT).
    Per-row error handling keeps one bad topology from aborting the batch. With
    dry_run=true nothing is written and the full report is returned.
    """
    raw = await file.read()
    actor_id = uuid.UUID(payload["sub"])
    actor_name = payload.get("username", "")
    actor_role = payload.get("role", "user")
    return await import_topologies(db, raw, format, dry_run, actor_id, actor_name, actor_role)
