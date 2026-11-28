"""Bulk import and export of devices and templates.

Export serializes rows to CSV or JSON. Import parses a file, resolves
cross-instance references by natural identity (template by name, driver by
name), validates each row against the existing Pydantic schemas, and calls the
existing create/update service functions row by row. Per-row error handling
means one bad row is rejected with a reason while the rest of the batch
proceeds. A `dry_run` import is a full rehearsal (issue #1017): it runs the
same create/update service functions on a
`herd_common.rehearsal.rehearsal_session`, inside a transaction that is rolled
back at the end, so the dry-run report matches the committing run row for row
and nothing is written.

Off the event loop (issue #1417): an import's decode, CSV or JSON parse, lone
surrogate scan, and the decode of every row's JSON cell (`field_data`, `sections`)
run on a worker thread through `herd_common.bulk_work.run_bulk_work` before the
first query (`_read_device_import`, `_read_template_import`); the row loop, its
schema checks, and every database call stay on the event loop in the request's
session. An export's rendering of its records into one body runs the same way.

There is no cross-schema bulk path: this lives in the inventory service and only
touches the inventory schema, matching the service-boundary rule.
"""

import csv
import io
import json
import uuid
from typing import Any

from fastapi import HTTPException
from herd_common.bulk_work import run_bulk_work
from herd_common.csv_import import read_csv_rows
from herd_common.csv_safety import csv_safe_cell, csv_unsafe_cell
from herd_common.rehearsal import rehearsal_session
from herd_common.validation_errors import holds_lone_surrogate
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.driver_package import DriverPackage
from app.models.hypervisor import Hypervisor
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
    "hypervisor_name",
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
    "hypervisor_name",
    "icon",
    "description",
    "vendor",
    "model",
    "part_number",
}

# A JSON import file, or a JSON cell of a CSV row, holding a lone surrogate (an escape
# such as \ud800 with no pair): a value no response, database write, or later read can
# encode. The whole JSON file is refused; a CSV row with such a cell is rejected.
IMPORT_NOT_TEXT_DETAIL = (
    "Import file holds a lone surrogate escape, which is not valid text; fix it and retry"
)
JSON_CELL_NOT_TEXT_REASON = "a JSON cell holds a lone surrogate escape, which is not valid text"

# A JSON import file, or a JSON cell of a CSV row, that `json.loads` refuses for its
# shape rather than its syntax (issue #1511): nested deeper than the C parser's
# recursion budget (RecursionError), or holding an integer literal longer than
# Python's integer string conversion limit, 4,300 digits (a ValueError that is not a
# JSONDecodeError). Both answers are fixed and name no part of the file or the
# exception. The whole JSON file is refused; a CSV row with such a cell is rejected.
IMPORT_JSON_UNREADABLE_DETAIL = (
    "Invalid JSON: the document is nested too deeply or holds a number too long to read"
)
JSON_CELL_UNREADABLE_REASON = "a JSON cell is nested too deeply or holds a number too long to read"

# A JSON import item the row loop cannot read (issue #1573): an item that is not an
# object (a list, string, number, boolean, or null), or an object whose `name` is not
# a string. A non-object item, or a name that is a nonzero number, true, or a non-empty
# list or object, answered 500 at the first `.get` or `.strip`; a name that is 0,
# false, [], or {} was read as missing. The item is now a rejected row with one of
# these fixed reasons and the other rows are judged on their own. A CSV row is always
# an object of strings, so it never meets either.
ROW_NOT_AN_OBJECT_REASON = "row is not an object"
NAME_NOT_A_STRING_REASON = "name: must be a string"


def _row_shape_reason(raw_row: Any) -> str | None:
    """The fixed reject reason for an item the row loop cannot read, else None.

    Checked before anything reads the item. A `name` that is absent or null passes
    here and is rejected later as `missing required field: name`.
    """
    if not isinstance(raw_row, dict):
        return ROW_NOT_AN_OBJECT_REASON
    name = raw_row.get("name")
    if name is not None and not isinstance(name, str):
        return NAME_NOT_A_STRING_REASON
    return None


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


def template_to_record(
    template: DeviceTemplate, hypervisor_names: dict[uuid.UUID, str] | None = None
) -> dict[str, Any]:
    """Serialize a template to an instance-portable record.

    The driver reference is emitted as `driver_name`, not the raw `driver_id`,
    and a dynamic template's hypervisor as `hypervisor_name` (issue #1024),
    resolved through `hypervisor_names` (id to name, built once by the
    exporter), so a dynamic template round-trips into another instance.
    """
    hypervisor_name = None
    if template.hypervisor_id is not None:
        hypervisor_name = (hypervisor_names or {}).get(template.hypervisor_id)
    return {
        "name": template.name,
        "template_type": template.template_type,
        "driver_name": template.driver.name if template.driver else None,
        "hypervisor_name": hypervisor_name,
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
    # read_csv_rows (issue #1574) reads every line ending, lifts the field limit for
    # the read, and answers 422 for anything the csv module still refuses.
    text_columns = text_columns or set()
    rows: list[dict[str, Any]] = []
    for row in read_csv_rows(raw):
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
        # The decoder's message names a position, never the file's content.
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except (ValueError, RecursionError):
        raise HTTPException(status_code=422, detail=IMPORT_JSON_UNREADABLE_DETAIL) from None
    if holds_lone_surrogate(doc):
        raise HTTPException(status_code=422, detail=IMPORT_NOT_TEXT_DETAIL)
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


class _DecodedCell:
    """A JSON cell already decoded on the parse thread (issue #1417): the value
    `_coerce_json_cell` returned for it there, or the text of the exception it
    raised. `_coerce_json_cell` hands the value back, or raises `_CellDecodeError`
    with that text, when the row loop reaches the cell, so the row's outcome and
    reason are the ones an on-loop decode gave. It keeps the text, never the
    exception: an exception's traceback holds the decoding frame, which holds every
    row, so each failed cell would keep the whole upload alive in a reference cycle
    until the garbage collector found it. It equals neither None nor "", as the
    non-empty text it stands for does not."""

    __slots__ = ("reason", "value")

    def __init__(self, value: Any, reason: str | None) -> None:
        self.value = value
        self.reason = reason


class _CellDecodeError(ValueError):
    """Raised where the row loop reads a cell whose decode failed on the parse
    thread. Its text is that decode's exception text, and the row loop rejects the
    row with it as it rejected the original (any exception that is neither an
    `HTTPException` nor a pydantic `ValidationError` lands in the same branch)."""


def _decode_json_cells(rows: list[Any], column: str, default: Any) -> None:
    """Decode, in place, each object row's non-empty text in `column` into a
    `_DecodedCell` (issue #1417). Runs on the parse thread with the file parse, so a
    cell as large as the file is not decoded on the event loop. Any other value
    (absent, null, empty, a native object or list, a number) is left for the row
    loop, which reads it without a parse. A failed decode keeps only its text, one
    string object for each distinct text, so a file of identical bad cells holds one
    reason, not one per row."""
    reasons: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if not isinstance(value, str) or value == "":
            continue
        try:
            row[column] = _DecodedCell(_coerce_json_cell(value, default), None)
        except Exception as exc:  # its text is raised again where the loop reads it
            reason = str(exc)
            row[column] = _DecodedCell(None, reasons.setdefault(reason, reason))


def _coerce_json_cell(value: Any, default: Any) -> Any:
    """Decode a CSV cell that carries JSON (field_data, sections).

    JSON import passes through native objects; CSV import passes a string we
    json.loads. An empty cell yields the supplied default. A cell the parser
    refuses for its depth or an over-long integer raises ValueError with the fixed
    JSON_CELL_UNREADABLE_REASON (issue #1511); a syntax error keeps the decoder's
    own message, which names a position, never the cell's content. A cell already
    decoded on the parse thread (`_DecodedCell`, issue #1417) gives back that
    decode's value, or raises `_CellDecodeError` with its exception's text.
    """
    if isinstance(value, _DecodedCell):
        if value.reason is not None:
            raise _CellDecodeError(value.reason)
        return value.value
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise
    except (ValueError, RecursionError):
        raise ValueError(JSON_CELL_UNREADABLE_REASON) from None
    if holds_lone_surrogate(parsed):
        raise ValueError(JSON_CELL_NOT_TEXT_REASON)
    return parsed


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


def _read_device_import(raw: bytes, fmt: str) -> list[Any]:
    """The device file parsed and its `field_data` cells decoded: the import's CPU
    work, run on a bulk worker thread (issue #1417)."""
    rows = parse_import(raw, fmt, DEVICE_CSV_COLUMNS, DEVICE_CSV_TEXT_COLUMNS)
    _decode_json_cells(rows, "field_data", {})
    return rows


async def import_devices(
    db: AsyncSession,
    raw: bytes,
    fmt: str,
    dry_run: bool,
    actor_id: uuid.UUID | None,
    actor_name: str | None,
) -> BulkImportReport:
    rows = await run_bulk_work(_read_device_import, raw, fmt)
    if dry_run:
        async with rehearsal_session(db) as rehearsal:
            report = await _import_device_rows(rehearsal, rows, actor_id, actor_name)
    else:
        report = await _import_device_rows(db, rows, actor_id, actor_name)
    report.dry_run = dry_run
    return report


async def _import_device_rows(
    db: AsyncSession,
    rows: list[dict[str, Any]],
    actor_id: uuid.UUID | None,
    actor_name: str | None,
) -> BulkImportReport:
    """Run every device row through the real create/update path on `db`
    (the request session, or a rehearsal session for a dry run)."""
    report = _empty_report(False)

    # Build a name -> template-id map once so reference resolution is O(1) per
    # row and does not issue a query per row. We store the plain id, not the ORM
    # instance: a per-row rollback (in the except branches below) expires every
    # instance in the session identity map, so a cached ORM object read on a
    # later row would trigger an implicit async lazy-refresh and raise. Only the
    # id is needed downstream, so this sidesteps the problem entirely.
    template_rows = (await db.execute(select(DeviceTemplate))).scalars().all()
    template_ids_by_name = {t.name: t.id for t in template_rows}

    for index, raw_row in enumerate(rows):
        shape_reason = _row_shape_reason(raw_row)
        if shape_reason is not None:
            report.rows.append(RowResult(row=index, action="reject", reason=shape_reason))
            continue
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
                await update_device(
                    db, existing.id, update, modified_by=actor_id, modified_by_name=actor_name
                )
                report.rows.append(RowResult(row=index, action="update", identity=name))
        except HTTPException as exc:
            await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc.detail))
            )
        except ValidationError as exc:
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
            await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc))
            )

    return _tally(report)


# Import: templates ----------------------------------------------------------


def _read_template_import(raw: bytes, fmt: str) -> list[Any]:
    """The template file parsed and its `sections` cells decoded: the import's CPU
    work, run on a bulk worker thread (issue #1417)."""
    rows = parse_import(raw, fmt, TEMPLATE_CSV_COLUMNS, TEMPLATE_CSV_TEXT_COLUMNS)
    _decode_json_cells(rows, "sections", [])
    return rows


async def import_templates(
    db: AsyncSession,
    raw: bytes,
    fmt: str,
    dry_run: bool,
    actor_id: uuid.UUID | None,
) -> BulkImportReport:
    rows = await run_bulk_work(_read_template_import, raw, fmt)
    if dry_run:
        async with rehearsal_session(db) as rehearsal:
            report = await _import_template_rows(rehearsal, rows, actor_id)
    else:
        report = await _import_template_rows(db, rows, actor_id)
    report.dry_run = dry_run
    return report


async def _import_template_rows(
    db: AsyncSession,
    rows: list[dict[str, Any]],
    actor_id: uuid.UUID | None,
) -> BulkImportReport:
    """Run every template row through the real create/update path on `db`
    (the request session, or a rehearsal session for a dry run)."""
    report = _empty_report(False)

    # Store the plain driver id, not the ORM instance: a per-row rollback below
    # expires every session-cached instance, so reading an attribute off a
    # cached ORM object on a later row would trigger an async lazy-refresh and
    # raise. Only the id is needed downstream.
    driver_rows = (await db.execute(select(DriverPackage))).scalars().all()
    driver_ids_by_name = {d.name: d.id for d in driver_rows}
    # issue #1024: a dynamic template's hypervisor resolves by name the same way.
    hypervisor_rows = (await db.execute(select(Hypervisor.name, Hypervisor.id))).all()
    hypervisor_ids_by_name = {hv_name: hv_id for hv_name, hv_id in hypervisor_rows}

    for index, raw_row in enumerate(rows):
        shape_reason = _row_shape_reason(raw_row)
        if shape_reason is not None:
            report.rows.append(RowResult(row=index, action="reject", reason=shape_reason))
            continue
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

            hypervisor_id: uuid.UUID | None = None
            hypervisor_name = (raw_row.get("hypervisor_name") or "").strip()
            if hypervisor_name:
                hypervisor_id = hypervisor_ids_by_name.get(hypervisor_name)
                if hypervisor_id is None:
                    report.rows.append(
                        RowResult(
                            row=index,
                            action="reject",
                            identity=name,
                            reason=f"hypervisor not found by name: {hypervisor_name!r}",
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
                    hypervisor_id=hypervisor_id,
                    exclusive=exclusive,
                    icon=raw_row.get("icon") or None,
                    description=raw_row.get("description") or None,
                    vendor=raw_row.get("vendor") or None,
                    model=raw_row.get("model") or None,
                    part_number=raw_row.get("part_number") or None,
                    sections=sections,
                    poll_interval_seconds=poll,
                )
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
                if hypervisor_id is not None:
                    update_kwargs["hypervisor_id"] = hypervisor_id
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
                await update_template(db, existing.id, update, modified_by=actor_id)
                report.rows.append(RowResult(row=index, action="update", identity=name))
        except HTTPException as exc:
            await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc.detail))
            )
        except ValidationError as exc:
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
            await db.rollback()
            report.rows.append(
                RowResult(row=index, action="reject", identity=name or None, reason=str(exc))
            )

    return _tally(report)
