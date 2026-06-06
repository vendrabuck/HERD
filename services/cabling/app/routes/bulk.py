"""Bulk import and export endpoints for topologies.

Export streams every topology as CSV (a flat edge list) or JSON (the full canvas
with device references carried by name). Import accepts an uploaded file,
resolves device names to local ids via the inventory service, recreates each
topology, and runs the existing validator so an unreachable edge is rejected.
A dry_run import writes nothing and returns the per-row report.

RBAC matches the interactive topology surface: creating topologies is open to any
authenticated user, so import is too; export likewise. The per-topology
creator-or-admin gate on the existing endpoints still governs later edits.
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

    Each topology is validated through the existing build_adjacency_graph /
    validate path before any write; one with an unreachable edge is rejected.
    Per-row error handling keeps one bad topology from aborting the batch. With
    dry_run=true nothing is written and the full report is returned.
    """
    raw = await file.read()
    actor_id = uuid.UUID(payload["sub"])
    actor_name = payload.get("username", "")
    return await import_topologies(db, raw, format, dry_run, actor_id, actor_name)
