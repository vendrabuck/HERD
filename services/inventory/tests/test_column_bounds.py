"""Request field bounds taken from the columns they are written to (INV-COL-1 to
INV-COL-4, INV-POLL-5).

Unit: a table of every bounded field of the device, template, port, device group,
and hypervisor request schemas. A string at its column's length passes and one
character more is the exact 422 item; a NUL character in any string field written
to a text or varchar column is the exact item; a poll interval at the `integer`
column's largest value passes and one more is the exact item; a number that is not
finite anywhere in `field_data` or a section field's default is the exact item.
Two completeness checks tie the table to the code: every string, integer, and JSON
field of those schemas is in the table or named as exempt with its reason, and every
`String(n)` column of those tables is bounded by exactly `n` or named as exempt.

Functional: the same refusals through the real interactive routes, each answering
422 with exactly the one item naming the field, and through both importers, CSV and
JSON, where each row's reason is `<field>: <message>` and carries no input value.

The unit suites run on SQLite, which enforces no column length, so the proof that
each bound is not looser than its real column is the Postgres-live suite
`test_bulk_import_reasons_live_pg.py`.
"""

import io
import json
import types
import typing
import uuid
from typing import Annotated, Any

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device
from app.models.device_group import DeviceGroup
from app.models.hypervisor import Hypervisor
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.column_bounds import INT32_MAX, NON_FINITE_NUMBER_MESSAGE, NUL_CHARACTER_MESSAGE
from app.schemas.device import DeviceCreate, DeviceUpdate, InternalDeviceCreate
from app.schemas.device_group import DeviceGroupCreate, DeviceGroupUpdate
from app.schemas.hypervisor import HypervisorCreate, HypervisorUpdate
from app.schemas.port import BulkPortCreate, PortCreate, PortUpdate
from app.schemas.template import FieldDefinition, SectionDefinition, TemplateCreate, TemplateUpdate
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import Enum, Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_SECTION = {"name": "S", "fields": [{"key": "n", "label": "N", "type": "number"}]}
_ID = "00000000-0000-0000-0000-0000000000a1"

# The smallest valid body of each schema; a case adds or replaces one field.
_BASE: dict[type[BaseModel], dict[str, Any]] = {
    DeviceCreate: {"name": "d", "template_id": _ID, "topology_type": "PHYSICAL"},
    DeviceUpdate: {},
    InternalDeviceCreate: {"template_id": _ID, "reservation_id": _ID},
    TemplateCreate: {"name": "t", "template_type": "port", "sections": [_SECTION]},
    TemplateUpdate: {},
    PortCreate: {"name": "p", "template_id": _ID},
    BulkPortCreate: {"name_prefix": "p", "starting_index": 0, "instances": 1, "template_id": _ID},
    PortUpdate: {},
    DeviceGroupCreate: {"name": "g"},
    DeviceGroupUpdate: {},
    HypervisorCreate: {"name": "h", "endpoint": "e", "hypervisor_type": "t", "secret_id": _ID},
    HypervisorUpdate: {},
}

# Every string field written to a `String(n)` column: (schema, field, column, bound).
# The bound is the column's length, except where the product's own bound is
# shorter (a bulk port prefix leaves room for the index, INV-PORT-5).
_STRING_FIELDS = [
    (DeviceCreate, "name", Device.__table__.c.name, 255),
    (DeviceUpdate, "name", Device.__table__.c.name, 255),
    (InternalDeviceCreate, "name", Device.__table__.c.name, 255),
    *(
        (schema, field, DeviceTemplate.__table__.c[field], 255)
        for schema in (TemplateCreate, TemplateUpdate)
        for field in ("name", "vendor", "model", "part_number")
    ),
    (PortCreate, "name", Port.__table__.c.name, 255),
    (PortUpdate, "name", Port.__table__.c.name, 255),
    (BulkPortCreate, "name_prefix", Port.__table__.c.name, 200),
    (DeviceGroupCreate, "name", DeviceGroup.__table__.c.name, 100),
    (DeviceGroupUpdate, "name", DeviceGroup.__table__.c.name, 100),
    *(
        (schema, field, Hypervisor.__table__.c[field], length)
        for schema in (HypervisorCreate, HypervisorUpdate)
        for field, length in (("name", 255), ("endpoint", 512), ("hypervisor_type", 50))
    ),
]
# Every string field written to a `Text` column: (schema, field, column, the
# product's own length bound or None).
_TEXT_FIELDS = [
    *(
        (schema, field, DeviceTemplate.__table__.c[field], None)
        for schema in (TemplateCreate, TemplateUpdate)
        for field in ("icon", "description")
    ),
    (DeviceGroupCreate, "description", DeviceGroup.__table__.c.description, 2000),
    (DeviceGroupUpdate, "description", DeviceGroup.__table__.c.description, 2000),
    (HypervisorCreate, "description", Hypervisor.__table__.c.description, None),
    (HypervisorUpdate, "description", Hypervisor.__table__.c.description, None),
]
# Every integer field written to an `Integer` column.
_INT_FIELDS = [
    (DeviceCreate, "poll_interval_seconds", Device.__table__.c.poll_interval_seconds),
    (DeviceUpdate, "poll_interval_seconds", Device.__table__.c.poll_interval_seconds),
    (TemplateCreate, "poll_interval_seconds", DeviceTemplate.__table__.c.poll_interval_seconds),
    (TemplateUpdate, "poll_interval_seconds", DeviceTemplate.__table__.c.poll_interval_seconds),
]
# Every field written to a `json` column.
_JSON_FIELDS = [
    (DeviceCreate, "field_data"),
    (DeviceUpdate, "field_data"),
    (InternalDeviceCreate, "field_data"),
    (PortCreate, "field_data"),
    (PortUpdate, "field_data"),
    (BulkPortCreate, "field_data"),
    (TemplateCreate, "sections"),
    (TemplateUpdate, "sections"),
]
# Fields of the covered schemas no column bound applies to, with the reason.
_EXEMPT_FIELDS = {
    (BulkPortCreate, "starting_index"): "builds the port names; the names are checked",
    (BulkPortCreate, "instances"): "a count of ports to create, at most 200",
    (TemplateCreate, "template_type"): "a Literal of three short values",
}
# String columns of the covered tables that no request schema writes.
_EXEMPT_COLUMNS = {
    "devices.created_by_name": "the caller's name from the token, not the request",
    "devices.modified_by_name": "the caller's name from the token, not the request",
    "device_templates.template_type": "written from TemplateCreate's three-value Literal",
}
_TABLES = (Device, DeviceTemplate, Port, DeviceGroup, Hypervisor)


def _case_id(schema: type, field: str) -> str:
    return f"{schema.__name__}.{field}"


def _errors(schema: type[BaseModel], body: dict) -> list[dict]:
    with pytest.raises(ValidationError) as exc:
        schema.model_validate(body)
    return [{"type": e["type"], "loc": e["loc"], "msg": e["msg"]} for e in exc.value.errors()]


def _too_long(field: str, bound: int) -> dict:
    return {
        "type": "string_too_long",
        "loc": (field,),
        "msg": f"String should have at most {bound} characters",
    }


def _nul(field: str) -> dict:
    return {"type": "value_error", "loc": (field,), "msg": f"Value error, {NUL_CHARACTER_MESSAGE}"}


def _over_int32(field: str) -> dict:
    return {
        "type": "less_than_equal",
        "loc": (field,),
        "msg": f"Input should be less than or equal to {INT32_MAX}",
    }


def _non_finite(*loc) -> dict:
    return {"type": "value_error", "loc": loc, "msg": f"Value error, {NON_FINITE_NUMBER_MESSAGE}"}


# Unit: the table ----------------------------------------------------------------


@pytest.mark.parametrize(
    "schema, field, column, bound",
    _STRING_FIELDS,
    ids=[_case_id(s, f) for s, f, _, _ in _STRING_FIELDS],
)
def test_a_string_at_its_bound_passes_and_one_past_is_refused(schema, field, column, bound):
    # Characters, not bytes: a varchar(n) column holds n characters of any width.
    schema.model_validate({**_BASE[schema], field: "\U0001d11e" * bound})
    assert _errors(schema, {**_BASE[schema], field: "x" * (bound + 1)}) == [_too_long(field, bound)]


@pytest.mark.parametrize(
    "schema, field, column, bound",
    _STRING_FIELDS,
    ids=[_case_id(s, f) for s, f, _, _ in _STRING_FIELDS],
)
def test_a_string_bound_is_its_column_length_or_shorter(schema, field, column, bound):
    assert isinstance(column.type, String)
    if (schema, field) == (BulkPortCreate, "name_prefix"):
        assert bound < column.type.length
    else:
        assert bound == column.type.length


@pytest.mark.parametrize(
    "schema, field",
    [(s, f) for s, f, _, _ in _STRING_FIELDS + _TEXT_FIELDS],
    ids=[_case_id(s, f) for s, f, _, _ in _STRING_FIELDS + _TEXT_FIELDS],
)
@pytest.mark.parametrize("value", ["\x00", "a\x00", "\x00b", "a\x00b\x00"])
def test_a_nul_character_in_a_text_field_is_refused(schema, field, value):
    assert _errors(schema, {**_BASE[schema], field: value}) == [_nul(field)]


@pytest.mark.parametrize(
    "schema, field, column, bound",
    _TEXT_FIELDS,
    ids=[_case_id(s, f) for s, f, _, _ in _TEXT_FIELDS],
)
def test_a_text_field_keeps_only_its_product_bound(schema, field, column, bound):
    assert not isinstance(column.type, String) or column.type.length is None
    schema.model_validate({**_BASE[schema], field: "x" * (bound or 100_000)})
    if bound is not None:
        assert _errors(schema, {**_BASE[schema], field: "x" * (bound + 1)}) == [
            _too_long(field, bound)
        ]


@pytest.mark.parametrize(
    "schema, field, column", _INT_FIELDS, ids=[_case_id(s, f) for s, f, _ in _INT_FIELDS]
)
def test_an_integer_at_the_column_maximum_passes_and_one_more_is_refused(schema, field, column):
    assert isinstance(column.type, Integer)
    assert schema.model_validate({**_BASE[schema], field: INT32_MAX}).model_dump()[field] == (
        INT32_MAX
    )
    for value in (INT32_MAX + 1, 2**40, 2**63):
        assert _errors(schema, {**_BASE[schema], field: value}) == [_over_int32(field)]


def _with_number(schema: type[BaseModel], field: str, number: Any) -> dict:
    if field == "sections":
        fields = [{"key": "n", "label": "N", "type": "number", "default": number}]
        return {**_BASE[schema], "sections": [{"name": "S", "fields": fields}]}
    return {**_BASE[schema], field: {"a": [{"b": number}]}}


def _json_loc(field: str) -> tuple:
    return ("sections", 0, "fields", 0, "default") if field == "sections" else (field,)


@pytest.mark.parametrize("schema, field", _JSON_FIELDS, ids=[_case_id(*c) for c in _JSON_FIELDS])
@pytest.mark.parametrize(
    "number", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_a_number_that_is_not_finite_in_a_json_field_is_refused(schema, field, number):
    assert _errors(schema, _with_number(schema, field, number)) == [_non_finite(*_json_loc(field))]


@pytest.mark.parametrize("schema, field", _JSON_FIELDS, ids=[_case_id(*c) for c in _JSON_FIELDS])
@pytest.mark.parametrize("number", [1.5, -0.0, 1e308, 10**30, True], ids=str)
def test_a_finite_number_in_a_json_field_passes(schema, field, number):
    schema.model_validate(_with_number(schema, field, number))


def test_the_non_finite_scan_needs_no_recursion():
    nested: Any = float("nan")
    for _ in range(100_000):
        nested = [nested]
    assert _errors(DeviceCreate, {**_BASE[DeviceCreate], "field_data": {"a": nested}}) == [
        _non_finite("field_data")
    ]


def test_a_section_field_default_that_is_not_finite_is_refused_on_its_own():
    for number in (float("nan"), float("inf")):
        with pytest.raises(ValidationError) as exc:
            FieldDefinition(key="n", label="N", type="number", default=number)
        assert [e["msg"] for e in exc.value.errors()] == [
            f"Value error, {NON_FINITE_NUMBER_MESSAGE}"
        ]
    SectionDefinition(name="S", fields=[FieldDefinition(key="n", label="N", type="number")])


# Unit: the table is complete -----------------------------------------------------


def _base_types(annotation: Any) -> set[type]:
    """The plain types an annotation admits: Annotated and unions unwrapped."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _base_types(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return set().union(*(_base_types(arg) for arg in typing.get_args(annotation)))
    return {origin or annotation}


def test_every_field_of_the_covered_schemas_is_in_the_table_or_exempt():
    covered = {(s, f) for s, f, _, _ in _STRING_FIELDS + _TEXT_FIELDS}
    covered |= {(s, f) for s, f, _ in _INT_FIELDS} | set(_JSON_FIELDS)
    missing = []
    for schema in _BASE:
        for field, info in schema.model_fields.items():
            kinds = _base_types(info.annotation) & {str, int, dict, list}
            if kinds and (schema, field) not in covered and (schema, field) not in _EXEMPT_FIELDS:
                missing.append(_case_id(schema, field))
    assert missing == []


def test_every_string_column_of_the_covered_tables_is_bounded_or_exempt():
    bounded = {f"{c.table.name}.{c.name}" for _, _, c, _ in _STRING_FIELDS}
    unbounded = []
    for model in _TABLES:
        for column in model.__table__.columns:
            key = f"{model.__table__.name}.{column.name}"
            # An Enum column is a String to SQLAlchemy; its schema field is the enum.
            if isinstance(column.type, Enum):
                continue
            if isinstance(column.type, String) and column.type.length is not None:
                if key not in bounded and key not in _EXEMPT_COLUMNS:
                    unbounded.append(key)
    assert unbounded == []


# Functional: through the interactive routes ---------------------------------------


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def _setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": "00000000-0000-0000-0000-000000000001",
        "username": "testadmin",
        "role": "admin",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_ROUTES: dict[type[BaseModel], tuple[str, str]] = {
    DeviceCreate: ("POST", "/devices"),
    DeviceUpdate: ("PUT", f"/devices/{_ID}"),
    InternalDeviceCreate: ("POST", "/devices/internal"),
    TemplateCreate: ("POST", "/templates"),
    TemplateUpdate: ("PUT", f"/templates/{_ID}"),
    PortCreate: ("POST", f"/devices/{_ID}/ports"),
    BulkPortCreate: ("POST", f"/devices/{_ID}/ports/bulk"),
    PortUpdate: ("PUT", f"/ports/{_ID}"),
    DeviceGroupCreate: ("POST", "/device-groups"),
    DeviceGroupUpdate: ("PUT", f"/device-groups/{_ID}"),
    HypervisorCreate: ("POST", "/hypervisors"),
    HypervisorUpdate: ("PUT", f"/hypervisors/{_ID}"),
}
_SENTINEL = "column-bound-marker-5d2e"


def _route_cases() -> list:
    cases = []
    for schema, field, _, bound in _STRING_FIELDS:
        cases.append((schema, field, _SENTINEL + "x" * bound, _too_long(field, bound)))
    for schema, field, _, _ in _STRING_FIELDS + _TEXT_FIELDS:
        cases.append((schema, field, f"{_SENTINEL}\x00", _nul(field)))
    for schema, field, _ in _INT_FIELDS:
        cases.append((schema, field, INT32_MAX + 1, _over_int32(field)))
    for schema, field in _JSON_FIELDS:
        body = _with_number(schema, field, float("nan"))
        cases.append((schema, field, body[field], _non_finite(*_json_loc(field))))
    return cases


_ROUTE_CASES = _route_cases()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema, field, value, item",
    _ROUTE_CASES,
    ids=[f"{_case_id(s, f)}-{i['type']}" for s, f, _, i in _ROUTE_CASES],
)
async def test_the_route_answers_the_one_422_item_naming_the_field(
    client, schema, field, value, item
):
    method, path = _ROUTES[schema]
    body = {**_BASE[schema], field: value}
    # Sent as text: json.dumps writes NaN, which a client's JSON encoder may refuse.
    resp = await client.request(
        method,
        path,
        content=json.dumps(body),
        headers={"content-type": "application/json", "x-internal-token": "t"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": [{**item, "loc": ["body", *item["loc"]]}]}
    assert _SENTINEL not in resp.text


async def _template(client, **overrides) -> dict:
    driver = await client.post(
        "/drivers",
        data={"name": f"drv-{uuid.uuid4().hex[:6]}", "connection_type": "Management"},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04x"), "application/zip")},
    )
    assert driver.status_code == 201, driver.text
    body = {
        "name": "Firewall",
        "driver_id": driver.json()["id"],
        "vendor": "V",
        "model": "M",
        "sections": [_SECTION],
        **overrides,
    }
    resp = await client.post("/templates", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _driver_storage():
    from unittest.mock import patch

    with (
        patch("app.services.driver_service.upload_object"),
        patch("app.services.driver_service.delete_object"),
    ):
        yield


@pytest.mark.asyncio
async def test_values_at_each_bound_are_accepted_through_the_routes(client):
    template = await _template(
        client,
        name="n" * 255,
        vendor="v" * 255,
        model="m" * 255,
        part_number="p" * 255,
        poll_interval_seconds=INT32_MAX,
    )
    assert template["poll_interval_seconds"] == INT32_MAX
    resp = await client.post(
        "/devices",
        json={
            "name": "d" * 255,
            "template_id": template["id"],
            "topology_type": "PHYSICAL",
            "field_data": {"n": 1e308},
            "poll_interval_seconds": INT32_MAX,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["poll_interval_seconds"] == INT32_MAX


# Functional: through both importers -----------------------------------------------


async def _import(client, resource: str, fmt: str, body: bytes) -> list[tuple]:
    resp = await client.post(
        f"/{resource}/import",
        params={"format": fmt},
        files={"file": (f"f.{fmt}", io.BytesIO(body), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert _SENTINEL not in resp.text
    return [(r["identity"], r["action"], r["reason"]) for r in resp.json()["rows"]]


def _reason(item: dict) -> str:
    return f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"


_FD = {"n": 1}


@pytest.mark.asyncio
async def test_the_device_importers_name_the_field_a_row_breaks(client):
    template = await _template(client)
    existing = {"name": "existing", "template_id": template["id"], "topology_type": "PHYSICAL"}
    assert (await client.post("/devices", json={**existing, "field_data": _FD})).status_code == 201
    base = {"template_name": "Firewall", "topology_type": "PHYSICAL", "field_data": _FD}
    long_name = "L" * 256
    items = [
        {**base, "name": long_name},
        {**base, "name": "nul\x00"},
        {**base, "name": "poll", "poll_interval_seconds": INT32_MAX + 1},
        {**base, "name": "existing", "poll_interval_seconds": INT32_MAX + 1},
        {**base, "name": "nan", "field_data": {"n": float("nan"), "m": _SENTINEL}},
        {**base, "name": "existing", "field_data": {"n": float("inf"), "m": _SENTINEL}},
        {**base, "name": "at-bound", "poll_interval_seconds": INT32_MAX},
    ]
    assert await _import(client, "devices", "json", json.dumps(items).encode()) == [
        (long_name, "reject", _reason(_too_long("name", 255))),
        ("nul\x00", "reject", _reason(_nul("name"))),
        ("poll", "reject", _reason(_over_int32("poll_interval_seconds"))),
        ("existing", "reject", _reason(_over_int32("poll_interval_seconds"))),
        ("nan", "reject", _reason(_non_finite("field_data"))),
        ("existing", "reject", _reason(_non_finite("field_data"))),
        ("at-bound", "create", None),
    ]

    csv_body = (
        "name,template_name,topology_type,field_data,poll_interval_seconds\n"
        f'{long_name},Firewall,PHYSICAL,"{{""n"": 1}}",\n'
        f'poll,Firewall,PHYSICAL,"{{""n"": 1}}",{INT32_MAX + 1}\n'
        f'existing,Firewall,PHYSICAL,"{{""n"": NaN, ""m"": ""{_SENTINEL}""}}",\n'
        f'at-bound,Firewall,PHYSICAL,"{{""n"": 1}}",{INT32_MAX}\n'
    ).encode()
    assert await _import(client, "devices", "csv", csv_body) == [
        (long_name, "reject", _reason(_too_long("name", 255))),
        ("poll", "reject", _reason(_over_int32("poll_interval_seconds"))),
        ("existing", "reject", _reason(_non_finite("field_data"))),
        ("at-bound", "update", None),
    ]


@pytest.mark.asyncio
async def test_the_template_importers_name_the_field_a_row_breaks(client):
    await _template(client, name="existing", template_type="port", driver_id=None)
    section_nan = [
        {
            "name": "S",
            "fields": [{"key": "n", "label": "N", "type": "number", "default": float("nan")}],
        }
    ]
    base = {"template_type": "port", "sections": [_SECTION], "description": _SENTINEL}
    items = [
        {**base, "name": "L" * 256},
        {**base, "name": "vendor", "vendor": "V" * 256},
        {**base, "name": "existing", "part_number": "P" * 256},
        {**base, "name": "desc", "description": f"{_SENTINEL}\x00"},
        {**base, "name": "icon", "icon": f"data:{_SENTINEL}\x00"},
        {**base, "name": "poll", "poll_interval_seconds": INT32_MAX + 1},
        {**base, "name": "nan", "sections": section_nan},
        {**base, "name": "existing", "sections": section_nan},
        {**base, "name": "at-bound", "vendor": "V" * 255, "poll_interval_seconds": INT32_MAX},
    ]
    default_loc = ("sections", 0, "fields", 0, "default")
    assert await _import(client, "templates", "json", json.dumps(items).encode()) == [
        ("L" * 256, "reject", _reason(_too_long("name", 255))),
        ("vendor", "reject", _reason(_too_long("vendor", 255))),
        ("existing", "reject", _reason(_too_long("part_number", 255))),
        ("desc", "reject", _reason(_nul("description"))),
        ("icon", "reject", _reason(_nul("icon"))),
        ("poll", "reject", _reason(_over_int32("poll_interval_seconds"))),
        ("nan", "reject", _reason(_non_finite(*default_loc))),
        ("existing", "reject", _reason(_non_finite(*default_loc))),
        ("at-bound", "create", None),
    ]
    csv_body = (
        "name,template_type,vendor,model,sections,poll_interval_seconds\n"
        f"model,port,V,{'M' * 256},,\n"
        f"poll,port,V,M,,{INT32_MAX + 1}\n"
        'nan2,port,V,M,"[{""name"": ""S"", ""fields"": [{""key"": ""n"", ""label"": ""N"",'
        ' ""type"": ""number"", ""default"": Infinity}]}]",\n'
    ).encode()
    assert await _import(client, "templates", "csv", csv_body) == [
        ("model", "reject", _reason(_too_long("model", 255))),
        ("poll", "reject", _reason(_over_int32("poll_interval_seconds"))),
        ("nan2", "reject", _reason(_non_finite(*default_loc))),
    ]
