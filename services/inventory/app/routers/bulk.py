"""Bulk import and export endpoints for devices and templates.

Export endpoints stream a CSV or JSON file of every device (dynamic-instance
devices excepted) or template. Import
endpoints accept an uploaded file, run per-row validation through the existing
create/update service functions, and return a per-row report. A `dry_run` import
writes nothing and returns the same report shape so an operator can preview the
outcome. RBAC matches the interactive create paths: import is admin-only;
export is available to any authenticated admin (it can expose field_data, which
may carry secrets).
"""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.device import Device
from app.models.hypervisor import Hypervisor
from app.models.template import DeviceTemplate
from app.schemas.bulk import BulkImportReport
from app.services.bulk_service import (
    DEVICE_CSV_COLUMNS,
    DEVICE_CSV_TEXT_COLUMNS,
    TEMPLATE_CSV_COLUMNS,
    TEMPLATE_CSV_TEXT_COLUMNS,
    device_to_record,
    import_devices,
    import_templates,
    records_to_csv,
    records_to_json,
    template_to_record,
)

router = APIRouter(tags=["bulk"])

_CSV_MEDIA = "text/csv"
_JSON_MEDIA = "application/json"

# A chunk well under the ceiling, so the read loop stops soon after passing it
# rather than reading a whole extra megabyte before noticing.
_UPLOAD_READ_CHUNK_BYTES = 64 * 1024

_BULK_TOO_LARGE_DETAIL = (
    "Import file exceeds the {limit} byte limit; export a smaller set and retry"
)


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload in chunks, refusing anything past ``limit`` (issue #1417).

    ``await file.read()`` had no bound: Starlette spools a multipart file to a
    temporary file and limits only non-file fields, and the gateway had no
    body-size limit either. The whole file therefore landed in memory, was
    decoded, and was parsed on the event loop — the loop every reservation and
    topology read depends on.

    Reading in chunks means an over-ceiling upload stops being read as soon as
    the ceiling is passed, and the refusal happens before any decode.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=_BULK_TOO_LARGE_DETAIL.format(limit=limit),
            )
    return b"".join(chunks)


def _export_response(records, columns, resource, fmt, text_columns=None):
    if fmt == "json":
        body = records_to_json(records, resource)
        media = _JSON_MEDIA
        ext = "json"
    else:
        body = records_to_csv(records, columns, text_columns)
        media = _CSV_MEDIA
        ext = "csv"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{resource}.{ext}"'},
    )


@router.get("/devices/export")
async def export_devices(
    format: str = Query("json", pattern="^(csv|json)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Export every device to CSV or JSON. Admin or superadmin only.

    Dynamic-instance devices (those carrying a `request_id`, created by
    execution for one booking) are left out: they have no life outside the
    reservation that made them, and their dynamic template cannot hold a
    device on import (issue #1068).
    """
    stmt = select(Device).where(Device.request_id.is_(None)).order_by(Device.name)
    devices = (await db.execute(stmt)).unique().scalars().all()
    records = [device_to_record(d) for d in devices]
    return _export_response(records, DEVICE_CSV_COLUMNS, "devices", format, DEVICE_CSV_TEXT_COLUMNS)


@router.post("/devices/import", response_model=BulkImportReport)
async def import_devices_endpoint(
    file: UploadFile = File(...),
    format: str = Query("json", pattern="^(csv|json)$"),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Import devices from CSV or JSON. Admin or superadmin only.

    Each row is validated and committed independently; one bad row is rejected
    with a reason without aborting the batch. With dry_run=true nothing is
    written and the full create/update/skip/reject report is returned.
    """
    raw = await _read_capped(file, settings.bulk_import_max_bytes)
    actor_id = uuid.UUID(payload["sub"])
    actor_name = payload.get("username", "")
    return await import_devices(db, raw, format, dry_run, actor_id, actor_name)


@router.get("/templates/export")
async def export_templates(
    format: str = Query("json", pattern="^(csv|json)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Export every template to CSV or JSON. Admin or superadmin only."""
    templates = (
        (await db.execute(select(DeviceTemplate).order_by(DeviceTemplate.name))).scalars().all()
    )
    hypervisor_names = {
        hv_id: hv_name
        for hv_id, hv_name in (await db.execute(select(Hypervisor.id, Hypervisor.name)))
    }
    records = [template_to_record(t, hypervisor_names) for t in templates]
    return _export_response(
        records, TEMPLATE_CSV_COLUMNS, "templates", format, TEMPLATE_CSV_TEXT_COLUMNS
    )


@router.post("/templates/import", response_model=BulkImportReport)
async def import_templates_endpoint(
    file: UploadFile = File(...),
    format: str = Query("json", pattern="^(csv|json)$"),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Import templates from CSV or JSON. Admin or superadmin only.

    Templates should be imported before devices so device imports can resolve
    their template_name references. Per-row error handling and dry_run match the
    device import endpoint.
    """
    raw = await _read_capped(file, settings.bulk_import_max_bytes)
    actor_id = uuid.UUID(payload["sub"])
    return await import_templates(db, raw, format, dry_run, actor_id)
