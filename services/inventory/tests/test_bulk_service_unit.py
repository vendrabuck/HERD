"""Direct unit tests for the synchronous helpers in app.services.bulk_service.

These target the parse/coerce primitives that run outside any DB-await, so they
exercise the rejection and format-error branches deterministically without a
session or HTTP transport.
"""

import json
import uuid

import pytest
from app.database import Base
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.services.bulk_service import (
    _coerce_bool,
    _coerce_int,
    _coerce_json_cell,
    _parse_csv,
    _parse_json,
    import_devices,
    import_templates,
    parse_import,
)
from fastapi import HTTPException
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
async def test_import_devices_bad_enum_rolls_back_via_generic_except(db_setup):
    """A bad status enum raises a Pydantic ValidationError (generic Exception
    branch), rejected with a rollback."""
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
async def test_import_templates_duplicate_in_batch_rejects_via_http_exception(db_setup):
    """Two rows with the same NEW name: the first creates, the second's create
    hits an IntegrityError surfaced as HTTPException(409) and is rejected with a
    rollback (the except-HTTPException branch of import_templates)."""
    section = {"name": "S", "fields": [{"key": "k", "label": "K", "type": "string"}]}
    row = {
        "name": "DupBatch",
        "template_type": "port",
        "exclusive": True,
        "sections": [section],
    }
    async with TestSessionLocal() as db:
        report = await import_templates(
            db,
            _json_bytes([dict(row), dict(row)]),
            "json",
            dry_run=False,
            actor_id=uuid.uuid4(),
        )
    assert report.created == 1
    assert report.rejected == 1
    assert "already exists" in [r.reason for r in report.rows if r.action == "reject"][0]


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
