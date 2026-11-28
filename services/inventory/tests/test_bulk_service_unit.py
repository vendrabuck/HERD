"""Direct unit tests for the synchronous helpers in app.services.bulk_service.

These target the parse/coerce primitives that run outside any DB-await, so they
exercise the rejection and format-error branches deterministically without a
session or HTTP transport.
"""

import csv
import gc
import io
import json
import sqlite3
import uuid
import weakref

import pytest
from app.database import Base
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.services.bulk_service import (
    JSON_CELL_NOT_JSON_REASON,
    NAME_NOT_A_STRING_REASON,
    POLL_NOT_A_WHOLE_NUMBER_REASON,
    ROW_NOT_AN_OBJECT_REASON,
    ROW_UNEXPECTED_REASON,
    _coerce_bool,
    _coerce_int,
    _coerce_json_cell,
    _decode_json_cells,
    _DecodedCell,
    _parse_csv,
    _parse_json,
    _row_shape_reason,
    _RowReasonError,
    import_devices,
    import_templates,
    parse_import,
    records_to_csv,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db_setup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _json_bytes(items) -> bytes:
    return json.dumps(items).encode()


# --- _parse_json ------------------------------------------------------------


def test_parse_json_invalid_document_raises_422():
    with pytest.raises(HTTPException) as exc:
        _parse_json("{not valid json")
    assert exc.value.status_code == 422
    assert "Invalid JSON" in exc.value.detail


def test_parse_json_accepts_bare_list():
    assert _parse_json('[{"name": "a"}]') == [{"name": "a"}]


def test_parse_json_accepts_items_wrapper():
    assert _parse_json('{"items": [{"name": "a"}]}') == [{"name": "a"}]


def test_parse_json_scalar_document_rejected():
    with pytest.raises(HTTPException) as exc:
        _parse_json("42")
    assert exc.value.status_code == 422
    assert "list of records" in exc.value.detail


def test_parse_json_items_not_a_list_rejected():
    with pytest.raises(HTTPException) as exc:
        _parse_json('{"items": {"name": "a"}}')
    assert exc.value.status_code == 422
    assert "'items' must be a list" in exc.value.detail


# --- parse_import dispatch --------------------------------------------------


def test_parse_import_unknown_format_rejected():
    with pytest.raises(HTTPException) as exc:
        parse_import(b"name\nx", "xml", ["name"])
    assert exc.value.status_code == 422
    assert "csv" in exc.value.detail and "json" in exc.value.detail


def test_parse_import_strips_utf8_bom_for_csv():
    # Prepend a UTF-8 BOM; parse_import decodes with utf-8-sig so the header
    # name is not corrupted by leading BOM bytes.
    raw = b"\xef\xbb\xbf" + b"name,template_name\nA,T"
    rows = parse_import(raw, "csv", ["name", "template_name"])
    assert rows == [{"name": "A", "template_name": "T"}]


def test_parse_csv_keeps_only_known_columns():
    rows = _parse_csv("name,extra\nA,drop", ["name"])
    assert rows == [{"name": "A"}]


# --- issue #1574: what the csv module could not read ------------------------


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_parse_csv_reads_every_line_ending(ending):
    rows = _parse_csv(ending.join(["name,model", "A,m1", "B,m2"]), ["name", "model"])
    assert rows == [{"name": "A", "model": "m1"}, {"name": "B", "model": "m2"}]


def test_parse_csv_reads_a_cell_past_the_default_field_limit():
    rows = _parse_csv("name\n" + "a" * 131_073 + "\n", ["name"])
    assert rows == [{"name": "a" * 131_073}]


def test_parse_import_answers_the_fixed_422_for_a_csv_error(monkeypatch):
    from herd_common import csv_import

    monkeypatch.setattr(csv_import, "_field_limit_for", lambda text: 4)
    with pytest.raises(HTTPException) as exc:
        parse_import(b"name\nA\nlonger\n", "csv", ["name"])
    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid CSV: line 3 could not be read; fix it and retry"


# --- issue #1573: an item the row loop cannot read --------------------------


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ([], "row is not an object"),
        (["a"], "row is not an object"),
        ("a", "row is not an object"),
        (1, "row is not an object"),
        (1.5, "row is not an object"),
        (True, "row is not an object"),
        (None, "row is not an object"),
        ({"name": 1}, "name: must be a string"),
        ({"name": False}, "name: must be a string"),
        ({"name": []}, "name: must be a string"),
        ({"name": {"a": "b"}}, "name: must be a string"),
        ({}, None),
        ({"name": None}, None),
        ({"name": ""}, None),
        ({"name": "A", "template_name": 1}, None),
    ],
)
def test_row_shape_reason(item, reason):
    assert _row_shape_reason(item) == reason
    if reason is not None:
        assert reason in (ROW_NOT_AN_OBJECT_REASON, NAME_NOT_A_STRING_REASON)


# --- issue #910: CSV formula-injection neutralization -----------------------


def test_records_to_csv_neutralizes_only_the_named_text_columns():
    records = [{"name": "=1+1", "template_name": "safe-name", "poll_interval_seconds": 30}]
    body = records_to_csv(
        records,
        ["name", "template_name", "poll_interval_seconds"],
        {"name", "template_name"},
    )
    row = list(csv.DictReader(io.StringIO(body)))[0]
    assert row["name"] == "'=1+1"
    # template_name carries no trigger, so it is untouched even though it is
    # a text column.
    assert row["template_name"] == "safe-name"
    # poll_interval_seconds is not in text_columns (a formatted number), so a
    # value that happens to look like a trigger there would still pass
    # through untouched; this column never carries one in practice.
    assert row["poll_interval_seconds"] == "30"


def test_records_to_csv_with_no_text_columns_is_a_no_op():
    records = [{"name": "=1+1"}]
    body = records_to_csv(records, ["name"])
    row = list(csv.DictReader(io.StringIO(body)))[0]
    assert row["name"] == "=1+1"


def test_records_to_csv_json_blob_column_never_quoted_even_if_named_a_text_column():
    # field_data/sections are JSON-encoded before the text_columns check ever
    # sees them (isinstance(value, (dict, list)) short-circuits first), so
    # naming a JSON column as a text_column by mistake could not corrupt it.
    records = [{"name": "=1+1", "field_data": {"a": 1}}]
    body = records_to_csv(records, ["name", "field_data"], {"name", "field_data"})
    row = list(csv.DictReader(io.StringIO(body)))[0]
    assert row["field_data"] == '{"a": 1}'


def test_parse_csv_strips_neutralizing_quote_from_named_text_columns():
    rows = _parse_csv(
        "name,template_name\n'=1+1,safe-name",
        ["name", "template_name"],
        {"name", "template_name"},
    )
    assert rows == [{"name": "=1+1", "template_name": "safe-name"}]


def test_parse_csv_leaves_apostrophe_led_value_alone():
    # A legitimately apostrophe-led name (no trigger after the quote) must
    # not be mangled by import.
    rows = _parse_csv("name\n'quoted", ["name"], {"name"})
    assert rows == [{"name": "'quoted"}]


def test_parse_csv_without_text_columns_does_not_strip_quotes():
    rows = _parse_csv("name\n'=1+1", ["name"])
    assert rows == [{"name": "'=1+1"}]


# --- _coerce_json_cell ------------------------------------------------------


def test_coerce_json_cell_empty_returns_default():
    assert _coerce_json_cell("", {"d": 1}) == {"d": 1}
    assert _coerce_json_cell(None, []) == []


def test_coerce_json_cell_passes_through_native_objects():
    obj = {"vlan": 100}
    assert _coerce_json_cell(obj, {}) is obj
    lst = [1, 2]
    assert _coerce_json_cell(lst, []) is lst


def test_coerce_json_cell_decodes_json_string():
    assert _coerce_json_cell('{"vlan": 100}', {}) == {"vlan": 100}


# --- _coerce_int ------------------------------------------------------------


def test_coerce_int_empty_is_none():
    assert _coerce_int("") is None
    assert _coerce_int(None) is None


def test_coerce_int_parses_string():
    assert _coerce_int("42") == 42


# --- _coerce_bool -----------------------------------------------------------


def test_coerce_bool_empty_returns_default():
    assert _coerce_bool("", True) is True
    assert _coerce_bool(None, False) is False


def test_coerce_bool_passthrough_native_bool():
    assert _coerce_bool(True, False) is True


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("y", True), ("0", False), ("no", False)],
)
def test_coerce_bool_string_truthiness(value, expected):
    assert _coerce_bool(value, False) is expected


# --- import_devices reject branches (DB-backed) -----------------------------


@pytest.mark.asyncio
async def test_import_devices_missing_template_name_rejected(db_setup):
    async with TestSessionLocal() as db:
        report = await import_devices(
            db,
            _json_bytes([{"name": "Dev1", "template_name": "   "}]),
            "json",
            dry_run=True,
            actor_id=None,
            actor_name=None,
        )
    assert report.rejected == 1
    row = report.rows[0]
    assert row.action == "reject"
    assert row.identity == "Dev1"
    assert "template_name" in row.reason


# --- import_templates branches (DB-backed) ----------------------------------


async def _seed_template(db, name: str) -> DeviceTemplate:
    tmpl = DeviceTemplate(
        name=name,
        template_type="device",
        vendor="Acme",
        model="M1",
        sections=[{"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}],
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@pytest.mark.asyncio
async def test_import_templates_missing_name_rejected(db_setup):
    async with TestSessionLocal() as db:
        report = await import_templates(
            db,
            _json_bytes([{"template_type": "device", "vendor": "A", "model": "M"}]),
            "json",
            dry_run=True,
            actor_id=None,
        )
    assert report.rejected == 1
    assert "name" in report.rows[0].reason


@pytest.mark.asyncio
async def test_import_templates_existing_name_is_update(db_setup):
    """An import row matching an existing template name takes the update path."""
    async with TestSessionLocal() as db:
        driver = DriverPackage(
            name="Drv",
            connection_type="Management",
            filename="d.zip",
            storage_key="k",
            size_bytes=1,
            sha256="abc",
            uploaded_by="admin",
        )
        db.add(driver)
        await _seed_template(db, "Existing")
        await db.commit()

        report = await import_templates(
            db,
            _json_bytes(
                [
                    {
                        "name": "Existing",
                        "template_type": "device",
                        "driver_name": "Drv",
                        "vendor": "NewVendor",
                        "model": "NewModel",
                        "sections": [
                            {
                                "name": "S",
                                "fields": [{"key": "k", "label": "K", "type": "string"}],
                            }
                        ],
                    }
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
    assert report.updated == 1
    assert report.created == 0


@pytest.mark.asyncio
async def test_import_templates_row_omitting_vendor_model_preserves_existing(db_setup):
    """Issue #283: an update row that omits vendor/model must leave the stored
    (NOT NULL) values untouched, not null them and fail the constraint. The row
    still applies the fields it does carry (here, a new description)."""
    async with TestSessionLocal() as db:
        tmpl = DeviceTemplate(
            name="Keep",
            template_type="device",
            vendor="Acme",
            model="M1",
            description="original",
            sections=[{"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}],
        )
        db.add(tmpl)
        await db.commit()
        tmpl_id = tmpl.id

        report = await import_templates(
            db,
            _json_bytes([{"name": "Keep", "description": "edited"}]),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
        assert report.updated == 1
        assert report.rejected == 0
        refreshed = await db.get(DeviceTemplate, tmpl_id)
        assert refreshed.vendor == "Acme"
        assert refreshed.model == "M1"
        assert refreshed.description == "edited"


@pytest.mark.asyncio
async def test_import_templates_omitting_exclusive_preserves_existing(db_setup):
    """A row that omits the exclusive column preserves the stored value rather
    than coercing the missing cell to the True default, which would silently
    flip a non-exclusive template."""
    async with TestSessionLocal() as db:
        tmpl = DeviceTemplate(
            name="Shared",
            template_type="device",
            vendor="Acme",
            model="M1",
            exclusive=False,
            sections=[{"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}],
        )
        db.add(tmpl)
        await db.commit()
        tmpl_id = tmpl.id

        report = await import_templates(
            db,
            _json_bytes([{"name": "Shared", "description": "x"}]),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
        assert report.updated == 1
        refreshed = await db.get(DeviceTemplate, tmpl_id)
        assert refreshed.exclusive is False


@pytest.mark.asyncio
async def test_import_templates_omitting_driver_preserves_existing(db_setup):
    """A row that omits driver_name preserves the stored driver_id rather than
    nulling it and orphaning the template from its driver."""
    async with TestSessionLocal() as db:
        driver = DriverPackage(
            name="KeepDrv",
            connection_type="Management",
            filename="d.zip",
            storage_key="k",
            size_bytes=1,
            sha256="abc",
            uploaded_by="admin",
        )
        db.add(driver)
        await db.commit()
        await db.refresh(driver)
        drv_id = driver.id
        tmpl = DeviceTemplate(
            name="HasDriver",
            template_type="device",
            driver_id=drv_id,
            vendor="Acme",
            model="M1",
            sections=[{"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}],
        )
        db.add(tmpl)
        await db.commit()
        tmpl_id = tmpl.id

        report = await import_templates(
            db,
            _json_bytes([{"name": "HasDriver", "description": "x"}]),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
        assert report.updated == 1
        refreshed = await db.get(DeviceTemplate, tmpl_id)
        assert refreshed.driver_id == drv_id


@pytest.mark.asyncio
async def test_import_devices_bad_enum_rolls_back_via_validation_error(db_setup):
    """A bad status enum raises a Pydantic ValidationError (its own branch since
    issue #1016), rejected with a rollback and a reason naming the field."""
    async with TestSessionLocal() as db:
        tmpl = await _seed_template(db, "DevTpl")
        report = await import_devices(
            db,
            _json_bytes(
                [
                    {
                        "name": "BadStatusDev",
                        "template_name": "DevTpl",
                        "topology_type": "PHYSICAL",
                        "status": "NOT_A_REAL_STATUS",
                        "field_data": {},
                    }
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
            actor_name="admin",
        )
        assert tmpl is not None
    assert report.rejected == 1
    assert report.rows[0].action == "reject"
    assert report.rows[0].reason.startswith("status: Input should be")


@pytest.mark.asyncio
async def test_import_devices_unknown_field_rolls_back_via_http_exception(db_setup):
    """create_device raises HTTPException(422) for unknown field_data keys; the
    live import rejects the row and rolls back (the except-HTTPException branch)."""
    async with TestSessionLocal() as db:
        await _seed_template(db, "DevTpl2")
        report = await import_devices(
            db,
            _json_bytes(
                [
                    {
                        "name": "UnknownFieldDev",
                        "template_name": "DevTpl2",
                        "topology_type": "PHYSICAL",
                        "status": "AVAILABLE",
                        "field_data": {"not_a_real_field": "x"},
                    }
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
            actor_name="admin",
        )
    assert report.rejected == 1
    assert "Unknown fields" in report.rows[0].reason


@pytest.mark.asyncio
async def test_import_templates_hypervisor_driver_on_device_rejects_via_http_exception(db_setup):
    """The except-HTTPException reject branch of import_templates. A row that
    points an existing device template at a Hypervisor-type driver makes
    update_template raise HTTPException(422) (device templates cannot use a
    Hypervisor driver, enforced by _validate_driver_connection_type). The row is
    rejected with its detail as the reason and a rollback, and the batch
    survives."""
    section = {"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}
    async with TestSessionLocal() as db:
        driver = DriverPackage(
            name="HvDrv",
            connection_type="Hypervisor",
            filename="d.zip",
            storage_key="k",
            size_bytes=1,
            sha256="abc",
            uploaded_by="admin",
        )
        db.add(driver)
        await _seed_template(db, "DeviceTpl")
        await db.commit()

        report = await import_templates(
            db,
            _json_bytes(
                [
                    {
                        "name": "DeviceTpl",
                        "template_type": "device",
                        "driver_name": "HvDrv",
                        "sections": [section],
                    }
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
    assert report.rejected == 1
    reason = [r.reason for r in report.rows if r.action == "reject"][0]
    assert "Hypervisor" in reason


@pytest.mark.asyncio
async def test_import_devices_failing_row_before_valid_row_still_creates(db_setup):
    """Regression for #76: a row that rolls back must not poison later valid
    rows referencing the same preloaded template. The template id is cached as a
    plain UUID, so the post-rollback lookup does not touch an expired ORM
    instance (which would lazy-refresh and raise under async SQLAlchemy)."""
    async with TestSessionLocal() as db:
        await _seed_template(db, "SharedTpl")
        report = await import_devices(
            db,
            _json_bytes(
                [
                    {
                        "name": "BadStatusDev",
                        "template_name": "SharedTpl",
                        "topology_type": "PHYSICAL",
                        "status": "NOT_A_REAL_STATUS",
                        "field_data": {},
                    },
                    {
                        "name": "GoodDev",
                        "template_name": "SharedTpl",
                        "topology_type": "PHYSICAL",
                        "status": "AVAILABLE",
                        "field_data": {},
                    },
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
            actor_name="admin",
        )
    assert report.created == 1
    assert report.rejected == 1
    created = [r for r in report.rows if r.action == "create"]
    assert [r.identity for r in created] == ["GoodDev"]


@pytest.mark.asyncio
async def test_import_templates_failing_row_before_valid_row_still_creates(db_setup):
    """Regression for #76: a row that rolls back must not poison later valid
    rows referencing the same preloaded driver. The driver id is cached as a
    plain UUID, so the post-rollback lookup does not touch an expired ORM
    instance."""
    section = {"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}
    async with TestSessionLocal() as db:
        driver = DriverPackage(
            name="SharedDrv",
            connection_type="Management",
            filename="d.zip",
            storage_key="k",
            size_bytes=1,
            sha256="abc",
            uploaded_by="admin",
        )
        db.add(driver)
        await db.commit()

        report = await import_templates(
            db,
            _json_bytes(
                [
                    # First row fails: 'device' template type with no driver_name
                    # fails TemplateCreate validation and rolls back.
                    {
                        "name": "BadDeviceTpl",
                        "template_type": "device",
                        "vendor": "A",
                        "model": "M",
                        "sections": [section],
                    },
                    # Second row is valid and references the same preloaded
                    # driver map; it must still be created post-rollback.
                    {
                        "name": "GoodDeviceTpl",
                        "template_type": "device",
                        "driver_name": "SharedDrv",
                        "vendor": "A",
                        "model": "M",
                        "sections": [section],
                    },
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
    assert report.created == 1
    assert report.rejected == 1
    created = [r for r in report.rows if r.action == "create"]
    assert [r.identity for r in created] == ["GoodDeviceTpl"]


@pytest.mark.asyncio
async def test_import_templates_invalid_row_rolls_back_and_rejects(db_setup):
    """A row that fails schema validation during a live import is rejected with
    a rollback, not propagated, so the batch survives."""
    async with TestSessionLocal() as db:
        # template_type 'device' with no driver_name fails TemplateCreate
        # validation (device templates must have a driver), exercising the
        # exception/rollback reject branch.
        report = await import_templates(
            db,
            _json_bytes(
                [
                    {
                        "name": "NoDriverDevice",
                        "template_type": "device",
                        "vendor": "A",
                        "model": "M",
                        "sections": [
                            {
                                "name": "S",
                                "fields": [{"key": "k", "label": "K", "type": "string"}],
                            }
                        ],
                    }
                ]
            ),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
    assert report.rejected == 1
    assert report.rows[0].action == "reject"


# The JSON cells decoded with the file (issue #1417) ----------------------------------


def test_decode_json_cells_wraps_text_cells_and_leaves_the_rest():
    """Mutation-checked: dropping the `value == ""` skip wraps the empty cell and
    fails this."""
    native = {"model": "x"}
    rows = [
        {"name": "text", "field_data": '{"model": "x"}'},
        {"name": "empty", "field_data": ""},
        {"name": "null", "field_data": None},
        {"name": "absent"},
        {"name": "native", "field_data": native},
        {"name": "number", "field_data": 5},
        ["not", "an", "object"],
    ]
    _decode_json_cells(rows, "field_data", {})
    assert isinstance(rows[0]["field_data"], _DecodedCell)
    assert _coerce_json_cell(rows[0]["field_data"], {}) == {"model": "x"}
    assert rows[1]["field_data"] == ""
    assert rows[2]["field_data"] is None
    assert "field_data" not in rows[3]
    assert rows[4]["field_data"] is native
    assert rows[5]["field_data"] == 5
    assert rows[6] == ["not", "an", "object"]


def test_a_decoded_cell_raises_what_the_decode_raised_where_the_row_reads_it():
    """The text is the original exception's; the exception is a fresh one, raised
    where the row loop reads the cell, which rejects the row with that text."""
    rows = [{"field_data": "{not json"}, {"field_data": '"\\ud800"'}]
    _decode_json_cells(rows, "field_data", {})
    with pytest.raises(ValueError) as syntax:
        _coerce_json_cell(rows[0]["field_data"], {})
    with pytest.raises(json.JSONDecodeError) as direct:
        _coerce_json_cell("{not json", {})
    assert str(syntax.value) == str(direct.value)
    assert not isinstance(syntax.value, ValidationError)
    with pytest.raises(ValueError) as surrogate:
        _coerce_json_cell(rows[1]["field_data"], {})
    assert str(surrogate.value) == (
        "a JSON cell holds a lone surrogate escape, which is not valid text"
    )


class _Marker:
    """A weakly referenceable object riding on a row, to see when the rows die."""


def test_a_failed_cell_decode_leaves_no_reference_cycle():
    """A failed cell keeps its text, never the exception: an exception's traceback
    holds the decoding frame, which holds every row, so the rows would outlive their
    last reference until the garbage collector ran. With the collector off, the rows
    die the moment the last reference goes. Mutation-checked: storing the exception
    itself on the cell fails this."""
    marker = _Marker()
    alive = weakref.ref(marker)
    rows = [{"field_data": "{not json", "marker": marker}, {"field_data": "[nope"}]
    del marker
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        _decode_json_cells(rows, "field_data", {})
        del rows
        assert alive() is None
    finally:
        if was_enabled:
            gc.enable()


def test_identical_failed_cells_share_one_reason_and_hold_no_exception():
    """A file of identical bad cells holds one reason string, however many rows, and
    no exception object. Mutation-checked: dropping the `setdefault` dedup fails this."""
    rows = [{"field_data": "{x"} for _ in range(1000)]
    _decode_json_cells(rows, "field_data", {})
    cells = [row["field_data"] for row in rows]
    assert len({id(cell.reason) for cell in cells}) == 1
    assert all(cell.value is None for cell in cells)
    assert not any(
        isinstance(ref, BaseException) for cell in cells for ref in gc.get_referents(cell)
    )


def test_a_decoded_cell_is_not_an_empty_cell():
    """The device update keeps a row's `field_data` only when the cell is neither
    None nor empty; a decoded cell stands for non-empty text."""
    assert _DecodedCell({}, None) not in (None, "")


# Fixed reasons for a row that fails outside the schema checks -------------------

_SENTINEL = "sentinel-3e9a6c1d0f7b4a52"
_REFUSED = "the database refused the row"
_NUL = "a value holds a character the database cannot store, such as a NUL"
_UNIQUE = f"{_REFUSED}: a value that must be unique is already in use"


class _DriverError(Exception):
    """A driver error shaped like asyncpg's adapter error: a SQLSTATE on it, a
    constraint name on the exception it wraps, and the test's marker value in both
    texts, which no reason may carry."""

    def __init__(self, sqlstate, constraint_name=None):
        super().__init__(f"value {_SENTINEL} refused")
        self.sqlstate = sqlstate
        cause = Exception(f"DETAIL: Key (name)=({_SENTINEL}) already exists.")
        cause.constraint_name = constraint_name
        self.__cause__ = cause


def _statement_error(orig, cls=None):
    return (cls or DBAPIError)(
        "INSERT INTO devices (name, field_data) VALUES ($1::VARCHAR, $2::JSON)",
        ("d", json.dumps({"password": _SENTINEL})),
        orig,
    )


_TRANSIENT = "the database was unavailable or busy; run the import again"

# The constraint name rides on the driver error (asyncpg keeps one there for a class
# 23 refusal); no reason carries it.
_SQLSTATE_CASES = [
    ("23505", "devices_name_key", _UNIQUE),
    ("23503", "devices_template_id_fkey", f"{_REFUSED}: a record the row refers to does not exist"),
    ("23502", None, f"{_REFUSED}: a required value is missing"),
    ("23514", "ck_poll", f"{_REFUSED}: a value is outside what the database allows"),
    ("23P01", "ex_overlap", f"{_REFUSED}: a value breaks a database constraint"),
    ("23505", _SENTINEL, _UNIQUE),
    ("22001", "devices_name_key", f"{_REFUSED}: a value is longer than its column allows"),
    ("22003", None, f"{_REFUSED}: a number is out of range for its column"),
    ("22021", None, f"{_REFUSED}: {_NUL}"),
    ("22P05", None, f"{_REFUSED}: {_NUL}"),
    ("22000", None, f"{_REFUSED}: a value does not fit the type of its column"),
    ("22P02", None, f"{_REFUSED}: a value does not fit the type of its column"),
    ("08000", None, _TRANSIENT),
    ("08003", None, _TRANSIENT),
    ("08006", None, _TRANSIENT),
    ("40001", None, _TRANSIENT),
    ("40P01", None, _TRANSIENT),
    ("40000", None, _TRANSIENT),
    ("53100", None, _TRANSIENT),
    ("53300", None, _TRANSIENT),
    ("57014", None, _TRANSIENT),
    ("57P01", None, _TRANSIENT),
    ("42P01", None, _REFUSED),
    ("XX000", None, _REFUSED),
    (None, None, _REFUSED),
]


async def _import_one_refused_row(importer: str, failure: Exception):
    """One row whose create raises `failure`, then one that is created."""
    from app.services import bulk_service

    section = {"name": "S", "fields": [{"key": "k", "label": "K", "type": "password"}]}
    async with TestSessionLocal() as db:
        if importer == "devices":
            await _seed_template(db, "Tpl")
            real = bulk_service.create_device
            base = {"template_name": "Tpl", "topology_type": "PHYSICAL", "field_data": {}}
        else:
            real = bulk_service.create_template
            base = {"template_type": "port", "vendor": "V", "model": "M", "sections": [section]}
        items = [{**base, "name": "bad"}, {**base, "name": "good"}]

        async def refusing(session, data, **kwargs):
            if data.name == "bad":
                raise failure
            return await real(session, data, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bulk_service, f"create_{importer[:-1]}", refusing)
            if importer == "devices":
                return await import_devices(db, _json_bytes(items), "json", False, None, "a")
            return await import_templates(db, _json_bytes(items), "json", False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("importer", ["devices", "templates"])
@pytest.mark.parametrize("sqlstate, constraint, reason", _SQLSTATE_CASES)
async def test_a_refused_row_is_named_by_its_sqlstate(
    db_setup, importer, sqlstate, constraint, reason
):
    failure = _statement_error(_DriverError(sqlstate, constraint))
    assert _SENTINEL in str(failure)  # the text the reason must not carry
    report = await _import_one_refused_row(importer, failure)
    assert [(r.identity, r.action, r.reason) for r in report.rows] == [
        ("bad", "reject", reason),
        ("good", "create", None),
    ]
    assert _SENTINEL not in report.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("importer", ["devices", "templates"])
@pytest.mark.parametrize("sqlstate", [None, "23505", "22001"], ids=["none", "23505", "22001"])
async def test_a_row_whose_connection_was_lost_is_named_unavailable(db_setup, importer, sqlstate):
    """A connection the driver reports lost names the database, not the row, whatever
    SQLSTATE rides on the error."""
    failure = DBAPIError(
        "INSERT INTO devices (name) VALUES ($1::VARCHAR)",
        (_SENTINEL,),
        _DriverError(sqlstate),
        connection_invalidated=True,
    )
    report = await _import_one_refused_row(importer, failure)
    assert [(r.identity, r.action, r.reason) for r in report.rows] == [
        ("bad", "reject", _TRANSIENT),
        ("good", "create", None),
    ]
    assert _SENTINEL not in report.model_dump_json()


def _sqlite_refusal(statement: str) -> sqlite3.Error:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE t (u TEXT UNIQUE, n TEXT NOT NULL, c INTEGER CHECK (c > 0),"
            " p INTEGER REFERENCES p (id))"
        )
        conn.execute(f"INSERT INTO t VALUES ('{_SENTINEL}', 'x', 1, NULL)")
        try:
            conn.execute(statement)
        except sqlite3.Error as exc:
            return exc
    finally:
        conn.close()
    raise AssertionError("the statement was not refused")


@pytest.mark.asyncio
@pytest.mark.parametrize("importer", ["devices", "templates"])
@pytest.mark.parametrize(
    "statement, reason",
    [
        (f"INSERT INTO t VALUES ('{_SENTINEL}', 'x', 1, NULL)", _UNIQUE),
        (
            f"INSERT INTO t VALUES ('{_SENTINEL}2', NULL, 1, NULL)",
            f"{_REFUSED}: a required value is missing",
        ),
        (
            f"INSERT INTO t VALUES ('{_SENTINEL}3', 'x', -1, NULL)",
            f"{_REFUSED}: a value is outside what the database allows",
        ),
        (
            f"INSERT INTO t VALUES ('{_SENTINEL}4', 'x', 1, 99)",
            f"{_REFUSED}: a record the row refers to does not exist",
        ),
    ],
    ids=["unique", "not-null", "check", "foreign-key"],
)
async def test_a_row_sqlite_refuses_is_named_by_its_error_code(
    db_setup, importer, statement, reason
):
    """SQLite gives no SQLSTATE; its extended error code names the same classes."""
    failure = _statement_error(_sqlite_refusal(statement), IntegrityError)
    assert _SENTINEL in str(failure)
    report = await _import_one_refused_row(importer, failure)
    assert (report.rows[0].action, report.rows[0].reason) == ("reject", reason)
    assert report.rows[1].action == "create"
    assert _SENTINEL not in report.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("importer", ["devices", "templates"])
@pytest.mark.parametrize(
    "make_failure, reason",
    [
        (lambda: RuntimeError(_SENTINEL), ROW_UNEXPECTED_REASON),
        (lambda: KeyError(_SENTINEL), ROW_UNEXPECTED_REASON),
        (lambda: TypeError(_SENTINEL), ROW_UNEXPECTED_REASON),
        (lambda: ValueError(_SENTINEL), ROW_UNEXPECTED_REASON),
        (lambda: AttributeError(_SENTINEL), ROW_UNEXPECTED_REASON),
        (lambda: InvalidRequestError(f"Object {_SENTINEL} is not bound"), _REFUSED),
        (lambda: HTTPException(status_code=409, detail={"v": _SENTINEL}), ROW_UNEXPECTED_REASON),
    ],
    ids=["runtime", "key", "type", "value", "attribute", "sqlalchemy", "http-detail-object"],
)
async def test_a_row_that_fails_otherwise_keeps_no_text(db_setup, importer, make_failure, reason):
    report = await _import_one_refused_row(importer, make_failure())
    assert (report.rows[0].action, report.rows[0].reason) == ("reject", reason)
    assert report.rows[1].action == "create"
    assert _SENTINEL not in report.model_dump_json()


@pytest.mark.parametrize(
    "value",
    ["abc", "1.5", "1e3", " ", _SENTINEL, "9" * 5000, [30], {"s": 30}, float("inf"), float("nan")],
    ids=[
        "word",
        "fraction",
        "exponent",
        "blank",
        "sentinel",
        "digits",
        "list",
        "object",
        "inf",
        "nan",
    ],
)
def test_a_poll_cell_that_is_not_a_whole_number_raises_the_fixed_reason(value):
    with pytest.raises(_RowReasonError) as exc:
        _coerce_int(value)
    assert str(exc.value) == POLL_NOT_A_WHOLE_NUMBER_REASON
    assert exc.value.__cause__ is None


@pytest.mark.parametrize("value", [5, 0, True, False, 1.5])
def test_a_json_cell_that_is_not_json_raises_the_fixed_reason(value):
    with pytest.raises(_RowReasonError) as exc:
        _coerce_json_cell(value, {})
    assert str(exc.value) == JSON_CELL_NOT_JSON_REASON


def test_a_cell_decode_that_fails_otherwise_keeps_the_chosen_reason(monkeypatch):
    """A decode on the parse thread that raises anything else keeps the fixed reason
    the row loop would choose, never the exception's text."""
    from app.services import bulk_service

    def failing(value, default):
        raise RuntimeError(f"decoder state {_SENTINEL}")

    monkeypatch.setattr(bulk_service, "_coerce_json_cell", failing)
    rows = [{"field_data": '{"k": "v"}'}]
    _decode_json_cells(rows, "field_data", {})
    assert rows[0]["field_data"].reason == ROW_UNEXPECTED_REASON
