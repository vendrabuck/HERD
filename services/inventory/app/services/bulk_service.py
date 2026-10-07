"""Bulk import and export of devices and templates.

Export serializes rows to CSV or JSON. Import parses a file, resolves
cross-instance references by natural identity (template by name, driver by
name), validates each row against the existing Pydantic schemas, and calls the
existing create/update service functions row by row. Per-row error handling
means one bad row is rejected with a reason while the rest of the batch
proceeds. A `dry_run` import runs full validation and reference resolution and
returns the per-row report without committing.

There is no cross-schema bulk path: this lives in the inventory service and only
touches the inventory schema, matching the service-boundary rule.
"""

import csv
import io
import json
import uuid
from typing import Any

from fastapi import HTTPException
from herd_common.csv_safety import csv_safe_cell, csv_unsafe_cell
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.schemas.bulk import BulkImportReport, RowResult
from app.schemas.device import DeviceCreate, DeviceUpdate
from app.schemas.template import TemplateCreate, TemplateUpdate
from app.services.inventory_service import create_device, update_device
from app.services.template_service import create_template, update_template

# Column order for device CSV export/import. field_data is JSON-encoded into a
# single cell so the CSV round-trips a nested object.
DEVICE_CSV_COLUMNS = [
    "name",
    "template_name",
    "topology_type",
    "status",
    "field_data",
    "poll_interval_seconds",
]

# Column order for template CSV export/import. sections is JSON-encoded into a
# single cell.
TEMPLATE_CSV_COLUMNS = [
    "name",
    "template_type",
    "driver_name",
    "exclusive",
    "icon",
    "description",
    "vendor",
    "model",
    "part_number",
    "sections",
    "poll_interval_seconds",
]

# issue #910: the columns above that carry free text and must be run through
# csv_safe_cell on export and csv_unsafe_cell on import. Excluded deliberately:
# topology_type, status, template_type (fixed enumerations), exclusive and
# poll_interval_seconds (formatted bool/number), and field_data/sections
# (JSON-encoded blobs whose first character is always "{" or "[", never a
# trigger, and which prefixing a quote onto would break json.loads on import).
DEVICE_CSV_TEXT_COLUMNS = {"name", "template_name"}
TEMPLATE_CSV_TEXT_COLUMNS = {
    "name",
    "driver_name",
    "icon",
    "description",
    "vendor",
    "model",
    "part_number",
}


def _empty_report(dry_run: bool) -> BulkImportReport:
    return BulkImportReport(
        dry_run=dry_run,
        total=0,
        created=0,
        updated=0,
        skipped=0,
        rejected=0,
        rows=[],
    )


def _tally(report: BulkImportReport) -> BulkImportReport:
    report.total = len(report.rows)
    report.created = sum(1 for r in report.rows if r.action == "create")
    report.updated = sum(1 for r in report.rows if r.action == "update")
    report.skipped = sum(1 for r in report.rows if r.action == "skip")
    report.rejected = sum(1 for r in report.rows if r.action == "reject")
    return report


# Export ---------------------------------------------------------------------


def device_to_record(device: Device) -> dict[str, Any]:
    """Serialize a device to an instance-portable record.

    The template reference is emitted as `template_name`, not the raw
    `template_id`, so the record imports into another instance whose template
    UUIDs differ.
    """
    return {
        "name": device.name,
        "template_name": device.template.name if device.template else None,
        "topology_type": device.topology_type.value
        if hasattr(device.topology_type, "value")
        else device.topology_type,
        "status": device.status.value if hasattr(device.status, "value") else device.status,
        "field_data": device.field_data or {},
        "poll_interval_seconds": device.poll_interval_seconds,
    }


def template_to_record(template: DeviceTemplate) -> dict[str, Any]:
    """Serialize a template to an instance-portable record.

    The driver reference is emitted as `driver_name`, not the raw `driver_id`.
    """
    return {
        "name": template.name,
        "template_type": template.template_type,
        "driver_name": template.driver.name if template.driver else None,
        "exclusive": template.exclusive,
        "icon": template.icon,
        "description": template.description,
        "vendor": template.vendor,
        "model": template.model,
        "part_number": template.part_number,
        "sections": template.sections or [],
        "poll_interval_seconds": template.poll_interval_seconds,
    }


def records_to_csv(
    records: list[dict[str, Any]], columns: list[str], text_columns: set[str] | None = None
) -> str:
    """Render records as CSV. `text_columns` (issue #910) names the columns to
    run through `csv_safe_cell`: free text that a caller other than the
    exporting admin may have written (a device name, a template's
    driver_name/description/vendor/model/...), never a JSON blob, a fixed
    enumeration, or a formatted number/bool.
    """
    text_columns = text_columns or set()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        row = {}
        for col in columns:
            value = rec.get(col)
            if isinstance(value, (dict, list)):
                row[col] = json.dumps(value)
            elif value is None:
                row[col] = ""
            elif col in text_columns:
                row[col] = csv_safe_cell(value)
            else:
                row[col] = value
        writer.writerow(row)
    return out.getvalue()


def records_to_json(records: list[dict[str, Any]], resource: str) -> str:
    return json.dumps({"resource": resource, "version": 1, "items": records}, indent=2)


# Import parsing -------------------------------------------------------------


def _parse_csv(
    raw: str, columns: list[str], text_columns: set[str] | None = None
) -> list[dict[str, Any]]:
    text_columns = text_columns or set()
    reader = csv.DictReader(io.StringIO(raw))
    rows: list[dict[str, Any]] = []
    for row in reader:
        # Strip the columns we do not recognize; keep the recognized ones.
        # A text_columns cell gets csv_unsafe_cell (issue #910), the inverse
        # of the csv_safe_cell neutralization records_to_csv applies, so an
        # exported name like "=1+1" round-trips through import unchanged.
        rows.append(
            {k: (csv_unsafe_cell(row.get(k)) if k in text_columns else row.get(k)) for k in columns}
        )
    return rows


def _parse_json(raw: str) -> list[dict[str, Any]]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if isinstance(doc, dict) and "items" in doc:
        items = doc["items"]
    elif isinstance(doc, list):
        items = doc
    else:
        raise HTTPException(
            status_code=422,
            detail="JSON import must be a list of records or an object with an 'items' list",
        )
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="'items' must be a list")
    return items


def parse_import(
    raw: bytes, fmt: str, columns: list[str], text_columns: set[str] | None = None
) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # A file saved in a legacy encoding (Latin-1, Windows-1252) is a client
        # input error, not a server fault (issue #1022).
        raise HTTPException(
            status_code=422,
            detail="Import file must be UTF-8 encoded; re-save it as UTF-8 and retry",
        ) from exc
    if fmt == "csv":
        # text_columns applies only to CSV: a JSON import was never run
        # through csv_safe_cell on export, so it carries no quote to strip.
        return _parse_csv(text, columns, text_columns)
    if fmt == "json":
        return _parse_json(text)
    raise HTTPException(status_code=422, detail="format must be 'csv' or 'json'")


def _coerce_json_cell(value: Any, default: Any) -> Any:
    """Decode a CSV cell that carries JSON (field_data, sections).

    JSON import passes through native objects; CSV import passes a string we
    json.loads. An empty cell yields the supplied default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _validation_reason(exc: ValidationError) -> str:
    """A row-rejection reason for a schema error: each failing field by name.

    str(ValidationError) carries the rejected input value, which for a device
    row is its whole field_data (credentials included), plus a documentation
    URL. The reason keeps only the field path and the message.
    """
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err["loc"]) or "row"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


# Import: devices ------------------------------------------------------------


async def import_devices(
    db: AsyncSession,
    raw: bytes,
    fmt: str,
    dry_run: bool,
    actor_id: uuid.UUID | None,
    actor_name: str | None,
) -> BulkImportReport:
    rows = parse_import(raw, fmt, DEVICE_CSV_COLUMNS, DEVICE_CSV_TEXT_COLUMNS)
    report = _empty_report(dry_run)

    # Build a name -> template-id map once so reference resolution is O(1) per
    # row and does not issue a query per row. We store the plain id, not the ORM
    # instance: a per-row rollback (in the except branches below) expires every
    # instance in the session identity map, so a cached ORM object read on a
    # later row would trigger an implicit async lazy-refresh and raise. Only the
    # id is needed downstream, so this sidesteps the problem entirely.
    template_rows = (await db.execute(select(DeviceTemplate))).scalars().all()
    template_ids_by_name = {t.name: t.id for t in template_rows}

    for index, raw_row in enumerate(rows):
        name = (raw_row.get("name") or "").strip()
        try:
            if not name:
                report.rows.append(
                    RowResult(row=index, action="reject", reason="missing required field: name")
                )
                continue

            template_name = (raw_row.get("template_name") or "").strip()
            if not template_name:
                report.rows.append(
                    RowResult(
                        row=index,
                        action="reject",
                        identity=name,
                        reason="missing required field: template_name",
                    )
                )
                continue
            template_id = template_ids_by_name.get(template_name)
            if template_id is None:
                report.rows.append(
                    RowResult(
                        row=index,
                        action="reject",
                        identity=name,
                        reason=f"template not found by name: {template_name!r}",
                    )
                )
                continue

            field_data = _coerce_json_cell(raw_row.get("field_data"), {})
            poll = _coerce_int(raw_row.get("poll_interval_seconds"))

            existing = (
                await db.execute(select(Device).where(Device.name == name))
            ).scalar_one_or_none()

            if existing is None:
                # A new device needs a topology type (a NOT NULL column with no
                # default); name the missing column rather than let the schema
                # report an enum mismatch on None (issue #1016).
                if raw_row.get("topology_type") in (None, ""):
                    report.rows.append(
                        RowResult(
                            row=index,
                            action="reject",
                            identity=name,
                            reason="missing required field: topology_type",
                        )
                    )
                    continue
                create = DeviceCreate(
                    name=name,
                    template_id=template_id,
                    topology_type=raw_row.get("topology_type"),
                    status=raw_row.get("status") or "AVAILABLE",
                    field_data=field_data,
                    poll_interval_seconds=poll,
                )
                if not dry_run:
                    await create_device(db, create, created_by=actor_id, created_by_name=actor_name)
                report.rows.append(RowResult(row=index, action="create", identity=name))
            else:
                # Build the update kwargs conditionally, as the template importer
                # does (issue #283): a column the row omits, or leaves empty, is
                # left out so update_device's exclude_unset keeps the stored
                # value. Passing every field explicitly overwrote omitted
                # field_data with {} (credentials included), cleared the poll
                # interval, and sent null into NOT NULL columns (issue #1016).
                update_kwargs: dict[str, Any] = {"name": name}
                for field in ("topology_type", "status"):
                    value = raw_row.get(field)
                    if value not in (None, ""):
                        update_kwargs[field] = value
                if raw_row.get("field_data") not in (None, ""):
                    update_kwargs["field_data"] = field_data
                if poll is not None:
                    update_kwargs["poll_interval_seconds"] = poll
                update = DeviceUpdate(**update_kwargs)
                if not dry_run:
                    await update_device(
                        db, existing.id, update, modified_by=actor_id, modified_by_name=actor_name
                    )
                report.rows.append(RowResult(row=index, action="update", identity=name))
        except HTTPException as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc.detail))
            )
        except ValidationError as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(
                    row=index,
                    action="reject",
                    identity=name or None,
                    reason=_validation_reason(exc),
                )
            )
        except Exception as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc))
            )

    return _tally(report)


# Import: templates ----------------------------------------------------------


async def import_templates(
    db: AsyncSession,
    raw: bytes,
    fmt: str,
    dry_run: bool,
    actor_id: uuid.UUID | None,
) -> BulkImportReport:
    rows = parse_import(raw, fmt, TEMPLATE_CSV_COLUMNS, TEMPLATE_CSV_TEXT_COLUMNS)
    report = _empty_report(dry_run)

    # Store the plain driver id, not the ORM instance: a per-row rollback below
    # expires every session-cached instance, so reading an attribute off a
    # cached ORM object on a later row would trigger an async lazy-refresh and
    # raise. Only the id is needed downstream.
    driver_rows = (await db.execute(select(DriverPackage))).scalars().all()
    driver_ids_by_name = {d.name: d.id for d in driver_rows}

    for index, raw_row in enumerate(rows):
        name = (raw_row.get("name") or "").strip()
        try:
            if not name:
                report.rows.append(
                    RowResult(row=index, action="reject", reason="missing required field: name")
                )
                continue

            driver_id: uuid.UUID | None = None
            driver_name = (raw_row.get("driver_name") or "").strip()
            if driver_name:
                driver_id = driver_ids_by_name.get(driver_name)
                if driver_id is None:
                    report.rows.append(
                        RowResult(
                            row=index,
                            action="reject",
                            identity=name,
                            reason=f"driver not found by name: {driver_name!r}",
                        )
                    )
                    continue

            sections = _coerce_json_cell(raw_row.get("sections"), [])
            poll = _coerce_int(raw_row.get("poll_interval_seconds"))
            template_type = raw_row.get("template_type") or "device"
            exclusive = _coerce_bool(raw_row.get("exclusive"), True)

            existing = (
                await db.execute(select(DeviceTemplate).where(DeviceTemplate.name == name))
            ).scalar_one_or_none()

            if existing is None:
                create = TemplateCreate(
                    name=name,
                    template_type=template_type,
                    driver_id=driver_id,
                    exclusive=exclusive,
                    icon=raw_row.get("icon") or None,
                    description=raw_row.get("description") or None,
                    vendor=raw_row.get("vendor") or None,
                    model=raw_row.get("model") or None,
                    part_number=raw_row.get("part_number") or None,
                    sections=sections,
                    poll_interval_seconds=poll,
                )
                if not dry_run:
                    await create_template(db, create)
                report.rows.append(RowResult(row=index, action="create", identity=name))
            else:
                # Build the update kwargs conditionally: a field the row omits is
                # left out so update_template's exclude_unset preserves the stored
                # value. Coercing a missing field to None instead would overwrite
                # it, and for the NOT NULL columns (vendor, model, sections) fail
                # the constraint and reject an otherwise valid update (issue #283).
                # An exported file re-imported unedited must be a no-op update.
                update_kwargs: dict[str, Any] = {"name": name}
                if driver_id is not None:
                    update_kwargs["driver_id"] = driver_id
                exclusive_cell = raw_row.get("exclusive")
                if exclusive_cell is not None and exclusive_cell != "":
                    update_kwargs["exclusive"] = _coerce_bool(exclusive_cell, True)
                for field in ("icon", "description", "vendor", "model", "part_number"):
                    value = raw_row.get(field)
                    if value not in (None, ""):
                        update_kwargs[field] = value
                if sections:
                    update_kwargs["sections"] = sections
                if poll is not None:
                    update_kwargs["poll_interval_seconds"] = poll
                update = TemplateUpdate(**update_kwargs)
                if not dry_run:
                    await update_template(db, existing.id, update, modified_by=actor_id)
                report.rows.append(RowResult(row=index, action="update", identity=name))
        except HTTPException as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc.detail))
            )
        except ValidationError as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(
                    row=index,
                    action="reject",
                    identity=name or None,
                    reason=_validation_reason(exc),
                )
            )
        except Exception as exc:
            if not dry_run:
                await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc))
            )

    return _tally(report)
