"""Postgres-live proof of inventory's request bounds and the bulk importers' row
reasons.

The unit suites run on SQLite, which enforces no column length, stores a NUL
character, and takes any integer; Postgres does none of these. Through the real
routes, with the request session on Postgres:

- a value at each bound of section 8.17 (INV-COL-1 to INV-COL-4, INV-POLL-5) is
  stored by its real column and read back unchanged, on the interactive create
  routes for templates, devices, ports, device groups, and hypervisors, so no
  bound is looser than its column;
- a value one past a bound, a NUL character, and a number that is not finite answer
  422 with exactly the one item naming the field on the interactive routes, and
  nothing is written;
- the same values in an import row reject that row with `<field>: <message>`
  (INV-BULK-9), a dry run and a commit report the same rows (INV-BULK-11,
  INV-BULK-18), and the rows before and after are judged on their own (INV-BULK-7);
- a row the database refuses outside the create and update functions gets the
  fixed reason for its SQLSTATE (INV-BULK-28): a BEFORE trigger this suite creates
  raises 40001, 40P01, 57014, 53300, and 08006, which read as the database being
  unavailable or busy, and 22001 and 22P02, which read by their class 22 entries,
  with the error's message, detail, and hint set from the row's own `field_data`;
  a class 23 refusal raised the same way on an update is answered by
  `update_device`'s own detail;
- a driver deleted, or a device name taken, by another connection between the
  import's lookup and the insert is answered by the create functions' own fixed
  details (INV-TPL-14, INV-DEV-1), which the importer keeps.

Each test carries a marker value in the rows' other cells and asserts each outcome
and reason exactly, and that neither the response body nor any log record at INFO
and above carries the marker or any text from a database error.

Env contract identical to the other live suites (HERD_TEST_PG_DSN,
HERD_TEST_PG_REQUIRED). Used-ledger scoping (docs/specs/TESTING.md, issue #819):
every row this suite writes carries a random tag in its name; a foreign template and
device are seeded; every template, device, port, device group, and hypervisor it does
not own is snapshotted before and after each test and must be unchanged; its own and
its seeded rows are deleted in a finally each. The trigger is created and dropped by
its own test and refuses only rows whose name holds that test's tag.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device
from app.models.device_group import DeviceGroup
from app.models.driver_package import DriverPackage
from app.models.hypervisor import Hypervisor
from app.models.port import Port
from app.models.template import DeviceTemplate
from app.schemas.column_bounds import INT32_MAX, NON_FINITE_NUMBER_MESSAGE, NUL_CHARACTER_MESSAGE
from app.services import bulk_service, hypervisor_service
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn

# The unit-suite conftest sets DB_SCHEMA="", so the mapped tables carry no schema;
# the live database keeps them under "inventory".
_CONNECT_ARGS = {"server_settings": {"search_path": "inventory"}}
ADMIN = {"sub": "00000000-0000-0000-0000-00000000a002", "role": "admin", "username": "pg"}

pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")

_REFUSED = "the database refused the row"
_BUSY = "the database was unavailable or busy; run the import again"
# A character outside the BMP: four bytes in UTF-8, one character to varchar(n).
_WIDE = "\U0001d11e"
_SECTIONS = [
    {
        "name": "G",
        "fields": [
            {"key": "pw", "label": "pw", "type": "password"},
            {"key": "code", "label": "code", "type": "string"},
            {"key": "n", "label": "n", "type": "number"},
        ],
    }
]


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


@pytest.fixture
async def session_factory():
    engine = create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture
def sentinel(tag) -> str:
    return f"pg-live-value-{tag}"


# (model, the columns compared) for every table this suite writes.
_SNAPSHOT = (
    (Device, ("name", "field_data", "poll_interval_seconds", "updated_at")),
    (DeviceTemplate, ("name", "sections", "description", "part_number", "updated_at")),
    (Port, ("name", "field_data", "updated_at")),
    (DeviceGroup, ("name", "description", "updated_at")),
    (Hypervisor, ("name", "endpoint", "hypervisor_type", "updated_at")),
)


async def _foreign_snapshot(session_factory, tag: str) -> dict:
    snapshot = {}
    async with session_factory() as s:
        for model, columns in _SNAPSHOT:
            rows = await s.execute(
                select(model.id, *(getattr(model, c) for c in columns)).where(
                    ~model.name.contains(tag)
                )
            )
            snapshot[model.__tablename__] = {row[0]: tuple(row[1:]) for row in rows}
    return snapshot


async def _delete_named(session_factory, name_part: str) -> None:
    async with session_factory() as s:
        for model in (Port, Device, DeviceTemplate, DriverPackage, DeviceGroup, Hypervisor):
            await s.execute(delete(model).where(model.name.contains(name_part)))
        await s.commit()


@pytest.fixture
async def lab(session_factory, tag, sentinel):
    """This test's own driver, template, and device, plus a foreign template and
    device; every row not of this test is snapshotted, and this test's are deleted."""
    foreign_tag = uuid.uuid4().hex[:10]
    async with session_factory() as s:
        driver = DriverPackage(
            name=f"pg-live-drv-{tag}",
            connection_type="Management",
            filename="d.zip",
            storage_key=f"k-{tag}",
            size_bytes=1,
            sha256="0" * 64,
            uploaded_by="pg",
        )
        s.add(driver)
        await s.flush()
        template = DeviceTemplate(
            name=f"pg-live-tpl-{tag}", vendor="V", model="M", sections=_SECTIONS
        )
        port_template = DeviceTemplate(
            name=f"pg-live-port-tpl-{tag}",
            template_type="port",
            vendor="V",
            model="M",
            sections=_SECTIONS,
        )
        foreign_template = DeviceTemplate(
            name=f"pg-live-foreign-{foreign_tag}", vendor="V", model="M", sections=_SECTIONS
        )
        s.add_all([template, port_template, foreign_template])
        await s.flush()
        device = Device(
            name=f"pg-live-dev-{tag}",
            template_id=template.id,
            topology_type="PHYSICAL",
            field_data={"pw": "own"},
        )
        s.add_all(
            [
                device,
                Device(
                    name=f"pg-live-foreign-{foreign_tag}",
                    template_id=foreign_template.id,
                    topology_type="PHYSICAL",
                    field_data={"pw": "foreign"},
                ),
            ]
        )
        await s.commit()
    try:
        before = await _foreign_snapshot(session_factory, tag)
        assert before["devices"] and before["device_templates"]
        try:
            yield {
                "driver": driver.name,
                "driver_id": driver.id,
                "template": template.name,
                "template_id": template.id,
                "port_template_id": port_template.id,
                "device": device.name,
                "device_id": device.id,
            }
        finally:
            await _delete_named(session_factory, tag)
            assert await _foreign_snapshot(session_factory, tag) == before
    finally:
        await _delete_named(session_factory, foreign_tag)


@pytest.fixture
async def client(session_factory):
    async def pg_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = pg_db
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _send(client, method: str, path: str, body: dict) -> httpx.Response:
    # Sent as text: json.dumps writes NaN and Infinity, which a client's encoder
    # may refuse.
    return await client.request(
        method, path, content=json.dumps(body), headers={"content-type": "application/json"}
    )


async def _import(client, resource: str, items: list, dry_run: bool) -> httpx.Response:
    resp = await client.post(
        f"/{resource}/import",
        params={"format": "json", "dry_run": str(dry_run).lower()},
        files={"file": ("f.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _assert_clean(text: str, sentinel: str) -> None:
    assert sentinel not in text
    assert "INSERT" not in text
    assert "UPDATE" not in text
    assert "[parameters" not in text
    assert "[SQL" not in text
    assert "sqlalche.me" not in text


def _outcomes(resp: httpx.Response) -> list[tuple]:
    return [(r["identity"], r["action"], r["reason"]) for r in resp.json()["rows"]]


async def _names(session_factory, model, tag: str) -> set[str]:
    async with session_factory() as s:
        rows = await s.execute(select(model.name).where(model.name.contains(tag)))
        return set(rows.scalars())


def _too_long(field: str, bound: int) -> str:
    return f"{field}: String should have at most {bound} characters"


def _over_int32(field: str) -> str:
    return f"{field}: Input should be less than or equal to {INT32_MAX}"


_NUL_MSG = f"Value error, {NUL_CHARACTER_MESSAGE}"
_NON_FINITE_MSG = f"Value error, {NON_FINITE_NUMBER_MESSAGE}"


def _at(length: int, tag: str) -> str:
    """A value of exactly `length` characters that holds the tag."""
    return tag + _WIDE * (length - len(tag))


# Bounds: a value at each bound is stored by its real column ----------------------


async def test_values_at_each_bound_are_stored_by_the_real_columns(
    client, session_factory, lab, tag, monkeypatch
):
    monkeypatch.setattr(hypervisor_service, "validate_secret_exists", AsyncMock())
    template_body = {
        "name": _at(255, tag),
        "template_type": "port",
        "vendor": _WIDE * 255,
        "model": _WIDE * 255,
        "part_number": _WIDE * 255,
        "description": _WIDE * 5000,
        "icon": "data:" + "A" * 5000,
        "sections": [
            {
                "name": "G",
                "fields": [{"key": "n", "label": "n", "type": "number", "default": 1e308}],
            }
        ],
        "poll_interval_seconds": INT32_MAX,
    }
    # No committed device carries a poll interval, so no health poll picks one up;
    # the device column's largest value is proven by a dry-run import below, which
    # runs the same INSERT and UPDATE and rolls them back.
    device_body = {
        "name": _at(255, tag),
        "template_id": str(lab["template_id"]),
        "topology_type": "PHYSICAL",
        # A json column stores a NUL in a string, so field_data keeps one.
        "field_data": {"pw": "a\x00b", "n": 1e308},
    }
    group_body = {"name": _at(100, tag), "description": _WIDE * 2000}
    hypervisor_body = {
        "name": _at(255, tag),
        "endpoint": _WIDE * 512,
        "hypervisor_type": _WIDE * 50,
        "secret_id": str(uuid.uuid4()),
    }
    created = {}
    for key, path, body in (
        ("template", "/templates", template_body),
        ("device", "/devices", device_body),
        ("group", "/device-groups", group_body),
        ("hypervisor", "/hypervisors", hypervisor_body),
    ):
        resp = await _send(client, "POST", path, body)
        assert resp.status_code == 201, resp.text
        created[key] = resp.json()
    device_id = created["device"]["id"]
    port_body = {"name": _at(255, tag), "template_id": str(lab["port_template_id"])}
    resp = await _send(client, "POST", f"/devices/{device_id}/ports", port_body)
    assert resp.status_code == 201, resp.text
    bulk_body = {
        "name_prefix": _at(200, f"b{tag}"),
        "starting_index": 0,
        "instances": 1,
        "template_id": str(lab["port_template_id"]),
    }
    resp = await _send(client, "POST", f"/devices/{device_id}/ports/bulk", bulk_body)
    assert resp.status_code == 201, resp.text

    async with session_factory() as s:
        template = await s.get(DeviceTemplate, uuid.UUID(created["template"]["id"]))
        assert (template.name, template.vendor, template.model, template.part_number) == (
            template_body["name"],
            template_body["vendor"],
            template_body["model"],
            template_body["part_number"],
        )
        assert template.description == template_body["description"]
        assert template.sections[0]["fields"][0]["default"] == 1e308
        assert template.poll_interval_seconds == INT32_MAX
        device = await s.get(Device, uuid.UUID(device_id))
        assert (device.name, device.field_data) == (device_body["name"], device_body["field_data"])
        group = await s.get(DeviceGroup, uuid.UUID(created["group"]["id"]))
        assert (group.name, group.description) == (group_body["name"], group_body["description"])
        hypervisor = await s.get(Hypervisor, uuid.UUID(created["hypervisor"]["id"]))
        assert (hypervisor.name, hypervisor.endpoint, hypervisor.hypervisor_type) == (
            hypervisor_body["name"],
            hypervisor_body["endpoint"],
            hypervisor_body["hypervisor_type"],
        )
        ports = await s.execute(select(Port.name).where(Port.device_id == uuid.UUID(device_id)))
        assert set(ports.scalars()) == {port_body["name"], bulk_body["name_prefix"] + "0"}

    base = {"template_name": lab["template"], "topology_type": "PHYSICAL", "field_data": {}}
    items = [
        {**base, "name": f"poll-{tag}", "poll_interval_seconds": INT32_MAX},
        {**base, "name": lab["device"], "poll_interval_seconds": INT32_MAX},
    ]
    resp = await _import(client, "devices", items, dry_run=True)
    assert _outcomes(resp) == [
        (f"poll-{tag}", "create", None),
        (lab["device"], "update", None),
    ]
    async with session_factory() as s:
        own = await s.get(Device, lab["device_id"])
        assert own.poll_interval_seconds is None


async def test_values_past_a_bound_answer_422_on_the_interactive_routes(
    client, session_factory, lab, tag, sentinel, caplog
):
    caplog.set_level(logging.INFO)
    section = {"name": "G", "fields": [{"key": "pw", "label": "pw", "type": "password"}]}
    template = {"template_type": "port", "vendor": "V", "model": "M", "sections": [section]}
    device = {
        "template_id": str(lab["template_id"]),
        "topology_type": "PHYSICAL",
        "field_data": {"pw": sentinel},
    }
    nan_section = {
        "name": "G",
        "fields": [{"key": "n", "label": "n", "type": "number", "default": float("inf")}],
    }
    cases = [
        (
            "POST",
            "/devices",
            {**device, "name": f"poll-{tag}", "poll_interval_seconds": 2**40},
            "less_than_equal",
            ["poll_interval_seconds"],
            f"Input should be less than or equal to {INT32_MAX}",
        ),
        (
            "POST",
            "/devices",
            {**device, "name": f"nul\x00-{tag}"},
            "value_error",
            ["name"],
            _NUL_MSG,
        ),
        (
            "POST",
            "/devices",
            {**device, "name": f"nan-{tag}", "field_data": {"pw": sentinel, "n": float("nan")}},
            "value_error",
            ["field_data"],
            _NON_FINITE_MSG,
        ),
        (
            "PUT",
            f"/devices/{lab['device_id']}",
            {"field_data": {"pw": sentinel}, "poll_interval_seconds": 2**31},
            "less_than_equal",
            ["poll_interval_seconds"],
            f"Input should be less than or equal to {INT32_MAX}",
        ),
        (
            "POST",
            "/templates",
            {**template, "name": "L" * 256 + tag, "description": sentinel},
            "string_too_long",
            ["name"],
            "String should have at most 255 characters",
        ),
        (
            "POST",
            "/templates",
            {**template, "name": f"desc-{tag}", "description": f"{sentinel}\x00"},
            "value_error",
            ["description"],
            _NUL_MSG,
        ),
        (
            "POST",
            "/templates",
            {**template, "name": f"inf-{tag}", "description": sentinel, "sections": [nan_section]},
            "value_error",
            ["sections", 0, "fields", 0, "default"],
            _NON_FINITE_MSG,
        ),
        (
            "PUT",
            f"/templates/{lab['template_id']}",
            {"part_number": "P" * 300, "description": sentinel},
            "string_too_long",
            ["part_number"],
            "String should have at most 255 characters",
        ),
    ]
    for method, path, body, kind, loc, msg in cases:
        resp = await _send(client, method, path, body)
        assert resp.status_code == 422, resp.text
        assert resp.json() == {"detail": [{"type": kind, "loc": ["body", *loc], "msg": msg}]}
        _assert_clean(resp.text, sentinel)
    _assert_clean(caplog.text, sentinel)
    assert await _names(session_factory, Device, tag) == {lab["device"]}
    assert await _names(session_factory, DeviceTemplate, tag) == {
        lab["template"],
        f"pg-live-port-tpl-{tag}",
    }
    async with session_factory() as s:
        own = await s.get(Device, lab["device_id"])
        assert (own.field_data, own.poll_interval_seconds) == ({"pw": "own"}, None)
        own_template = await s.get(DeviceTemplate, lab["template_id"])
        assert (own_template.part_number, own_template.description) == (None, None)


# Import rows past a bound name the field ------------------------------------------


async def test_device_rows_past_a_column_bound_name_the_field(
    client, session_factory, lab, tag, sentinel, caplog
):
    caplog.set_level(logging.INFO)
    base = {"template_name": lab["template"], "topology_type": "PHYSICAL"}
    fd = {"pw": sentinel}
    items = [
        {**base, "name": f"before-{tag}", "field_data": fd},
        {**base, "name": f"int-{tag}", "field_data": fd, "poll_interval_seconds": 2**40},
        {**base, "name": f"nul\x00-{tag}", "field_data": fd},
        {**base, "name": f"nan-{tag}", "field_data": {"pw": sentinel, "n": float("nan")}},
        # An update of this test's own device.
        {**base, "name": lab["device"], "field_data": fd, "poll_interval_seconds": 2**31},
        {**base, "name": f"after-{tag}", "field_data": fd},
    ]
    expected = [
        (f"before-{tag}", "create", None),
        (f"int-{tag}", "reject", _over_int32("poll_interval_seconds")),
        (f"nul\x00-{tag}", "reject", f"name: {_NUL_MSG}"),
        (f"nan-{tag}", "reject", f"field_data: {_NON_FINITE_MSG}"),
        (lab["device"], "reject", _over_int32("poll_interval_seconds")),
        (f"after-{tag}", "create", None),
    ]
    rehearsal = await _import(client, "devices", items, dry_run=True)
    assert await _names(session_factory, Device, tag) == {lab["device"]}
    committed = await _import(client, "devices", items, dry_run=False)
    for resp in (rehearsal, committed):
        _assert_clean(resp.text, sentinel)
        assert _outcomes(resp) == expected
    _assert_clean(caplog.text, sentinel)
    assert "sqlstate=" not in caplog.text
    assert await _names(session_factory, Device, tag) == {
        lab["device"],
        f"before-{tag}",
        f"after-{tag}",
    }
    async with session_factory() as s:
        own = await s.get(Device, lab["device_id"])
        assert (own.field_data, own.poll_interval_seconds) == ({"pw": "own"}, None)


async def test_template_rows_past_a_column_bound_name_the_field(
    client, session_factory, lab, tag, sentinel, caplog
):
    caplog.set_level(logging.INFO)
    sections = [
        {
            "name": "G",
            "fields": [{"key": "pw", "label": "pw", "type": "password", "default": sentinel}],
        }
    ]
    inf_sections = [
        {
            "name": "G",
            "fields": [{"key": "n", "label": "n", "type": "number", "default": float("inf")}],
        }
    ]
    base = {"template_type": "port", "vendor": "V", "model": "M", "sections": sections}
    long_name = "L" * 256 + tag
    items = [
        {**base, "name": f"before-{tag}"},
        {**base, "name": long_name, "description": sentinel},
        {**base, "name": f"desc-{tag}", "description": f"{sentinel}\x00"},
        {**base, "name": f"vendor-{tag}", "vendor": "V" * 256, "description": sentinel},
        {**base, "name": f"inf-{tag}", "sections": inf_sections, "description": sentinel},
        # An update of this test's own template.
        {**base, "name": lab["template"], "template_type": "device", "part_number": "P" * 300},
        {**base, "name": f"after-{tag}"},
    ]
    expected = [
        (f"before-{tag}", "create", None),
        (long_name, "reject", _too_long("name", 255)),
        (f"desc-{tag}", "reject", f"description: {_NUL_MSG}"),
        (f"vendor-{tag}", "reject", _too_long("vendor", 255)),
        (f"inf-{tag}", "reject", f"sections.0.fields.0.default: {_NON_FINITE_MSG}"),
        (lab["template"], "reject", _too_long("part_number", 255)),
        (f"after-{tag}", "create", None),
    ]
    rehearsal = await _import(client, "templates", items, dry_run=True)
    assert await _names(session_factory, DeviceTemplate, tag) == {
        lab["template"],
        f"pg-live-port-tpl-{tag}",
    }
    committed = await _import(client, "templates", items, dry_run=False)
    for resp in (rehearsal, committed):
        _assert_clean(resp.text, sentinel)
        assert _outcomes(resp) == expected
    _assert_clean(caplog.text, sentinel)
    assert "sqlstate=" not in caplog.text
    assert await _names(session_factory, DeviceTemplate, tag) == {
        lab["template"],
        f"pg-live-port-tpl-{tag}",
        f"before-{tag}",
        f"after-{tag}",
    }
    async with session_factory() as s:
        own = await s.get(DeviceTemplate, lab["template_id"])
        assert own.part_number is None
        assert own.sections == _SECTIONS


# Refusals raised outside the create and update functions ---------------------------


@pytest.fixture
async def refusing_trigger(session_factory, lab, tag):
    """A BEFORE INSERT OR UPDATE trigger on devices that raises the SQLSTATE in a
    row's `field_data.code`, only for a row whose name holds this test's tag, with
    the error's message, detail, and hint all set from the row's `field_data`."""
    function = f"inventory.pg_live_refuse_{tag}"
    trigger = f"pg_live_refuse_{tag}"
    async with session_factory() as s:
        await s.execute(
            text(
                f"""
                CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF strpos(NEW.name, '{tag}') > 0 AND (NEW.field_data->>'code') IS NOT NULL
                  THEN
                    RAISE EXCEPTION USING
                      ERRCODE = NEW.field_data->>'code',
                      MESSAGE = NEW.field_data::text,
                      DETAIL = NEW.field_data::text,
                      HINT = NEW.field_data::text;
                  END IF;
                  RETURN NEW;
                END $$
                """
            )
        )
        await s.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON inventory.devices"
                f" FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
        )
        await s.commit()
    try:
        yield
    finally:
        async with session_factory() as s:
            await s.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON inventory.devices"))
            await s.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))
            await s.commit()


_TRIGGER_CASES = [
    ("40001", _BUSY),
    ("40P01", _BUSY),
    ("57014", _BUSY),
    ("53300", _BUSY),
    ("08006", _BUSY),
    ("22001", f"{_REFUSED}: a value is longer than its column allows"),
    ("22P02", f"{_REFUSED}: a value does not fit the type of its column"),
]


async def test_a_refusal_raised_outside_the_service_functions_gets_its_fixed_reason(
    client, session_factory, lab, tag, sentinel, refusing_trigger, caplog
):
    caplog.set_level(logging.INFO)
    base = {"template_name": lab["template"], "topology_type": "PHYSICAL"}
    items = [{**base, "name": f"before-{tag}", "field_data": {"pw": sentinel}}]
    items += [
        {**base, "name": f"code-{code}-{tag}", "field_data": {"pw": sentinel, "code": code}}
        for code, _ in _TRIGGER_CASES
    ]
    items += [
        # A class 23 refusal on an update: update_device answers it with its own detail.
        {**base, "name": lab["device"], "field_data": {"pw": sentinel, "code": "23514"}},
        {**base, "name": f"after-{tag}", "field_data": {"pw": sentinel}},
    ]
    expected = [(f"before-{tag}", "create", None)]
    expected += [(f"code-{code}-{tag}", "reject", reason) for code, reason in _TRIGGER_CASES]
    expected += [
        (lab["device"], "reject", "Device violates a database constraint"),
        (f"after-{tag}", "create", None),
    ]
    rehearsal = await _import(client, "devices", items, dry_run=True)
    assert await _names(session_factory, Device, tag) == {lab["device"]}
    committed = await _import(client, "devices", items, dry_run=False)
    for resp in (rehearsal, committed):
        _assert_clean(resp.text, sentinel)
        assert _outcomes(resp) == expected
    _assert_clean(caplog.text, sentinel)
    for code, _ in _TRIGGER_CASES:
        assert f"sqlstate={code}" in caplog.text
    assert await _names(session_factory, Device, tag) == {
        lab["device"],
        f"before-{tag}",
        f"after-{tag}",
    }
    async with session_factory() as s:
        own = await s.get(Device, lab["device_id"])
        assert own.field_data == {"pw": "own"}


async def test_a_template_whose_driver_goes_before_its_insert_gets_the_fixed_reason(
    client, session_factory, lab, tag, sentinel, caplog, monkeypatch
):
    """A foreign key violation (23503): another connection deletes the driver the
    row resolved by name, after the import's lookup and before the row's insert."""
    caplog.set_level(logging.INFO)
    real = bulk_service.create_template

    async def driver_goes_first(db, data, **kwargs):
        if data.driver_id == lab["driver_id"]:
            async with session_factory() as other:
                await other.execute(delete(DriverPackage).where(DriverPackage.id == data.driver_id))
                await other.commit()
        return await real(db, data, **kwargs)

    monkeypatch.setattr(bulk_service, "create_template", driver_goes_first)
    sections = [
        {
            "name": "G",
            "fields": [{"key": "pw", "label": "pw", "type": "password", "default": sentinel}],
        }
    ]
    base = {"vendor": "V", "model": "M", "description": sentinel, "sections": sections}
    items = [
        {**base, "name": f"fk-{tag}", "template_type": "device", "driver_name": lab["driver"]},
        {**base, "name": f"after-{tag}", "template_type": "port"},
    ]
    resp = await _import(client, "templates", items, dry_run=False)
    _assert_clean(resp.text, sentinel)
    _assert_clean(caplog.text, sentinel)
    assert _outcomes(resp) == [
        (f"fk-{tag}", "reject", "Referenced hypervisor or driver does not exist"),
        (f"after-{tag}", "create", None),
    ]
    assert await _names(session_factory, DeviceTemplate, tag) == {
        lab["template"],
        f"pg-live-port-tpl-{tag}",
        f"after-{tag}",
    }


async def test_a_device_name_taken_before_its_insert_gets_the_fixed_reason(
    client, session_factory, lab, tag, sentinel, caplog, monkeypatch
):
    """A unique violation (23505): another connection commits a device of the same
    name after the import's lookup by name and before the row's insert."""
    caplog.set_level(logging.INFO)
    real = bulk_service.create_device
    raced = f"raced-{tag}"

    async def name_taken_first(db, data, **kwargs):
        if data.name == raced:
            async with session_factory() as other:
                other.add(
                    Device(
                        name=raced,
                        template_id=data.template_id,
                        topology_type="PHYSICAL",
                        field_data={"pw": "first"},
                    )
                )
                await other.commit()
        return await real(db, data, **kwargs)

    monkeypatch.setattr(bulk_service, "create_device", name_taken_first)
    base = {"template_name": lab["template"], "topology_type": "PHYSICAL"}
    items = [
        {**base, "name": raced, "field_data": {"pw": sentinel}},
        {**base, "name": f"after-{tag}", "field_data": {"pw": "x"}},
    ]
    resp = await _import(client, "devices", items, dry_run=False)
    _assert_clean(resp.text, sentinel)
    _assert_clean(caplog.text, sentinel)
    assert _outcomes(resp) == [
        (raced, "reject", f"Device with name '{raced}' already exists"),
        (f"after-{tag}", "create", None),
    ]
    async with session_factory() as s:
        winner = (await s.execute(select(Device).where(Device.name == raced))).scalar_one()
        assert winner.field_data == {"pw": "first"}
