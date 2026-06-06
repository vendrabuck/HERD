"""Bulk import and export endpoints for devices and templates.

Export endpoints stream a CSV or JSON file of every device or template. Import
endpoints accept an uploaded file, run per-row validation through the existing
create/update service functions, and return a per-row report. A `dry_run` import
writes nothing and returns the same report shape so an operator can preview the
outcome. RBAC matches the interactive create paths: import is admin-only;
export is available to any authenticated admin (it can expose field_data, which
may carry secrets).
"""

import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.device import Device
from app.models.template import DeviceTemplate
from app.schemas.bulk import BulkImportReport
from app.services.bulk_service import (
    DEVICE_CSV_COLUMNS,
    TEMPLATE_CSV_COLUMNS,
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


def _export_response(records, columns, resource, fmt):
    if fmt == "json":
        body = records_to_json(records, resource)
        media = _JSON_MEDIA
        ext = "json"
    else:
        body = records_to_csv(records, columns)
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
    """Export every device to CSV or JSON. Admin or superadmin only."""
    devices = (await db.execute(select(Device).order_by(Device.name))).unique().scalars().all()
    records = [device_to_record(d) for d in devices]
    return _export_response(records, DEVICE_CSV_COLUMNS, "devices", format)


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
    raw = await file.read()
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
    records = [template_to_record(t) for t in templates]
    return _export_response(records, TEMPLATE_CSV_COLUMNS, "templates", format)


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
    raw = await file.read()
    actor_id = uuid.UUID(payload["sub"])
    return await import_templates(db, raw, format, dry_run, actor_id)
