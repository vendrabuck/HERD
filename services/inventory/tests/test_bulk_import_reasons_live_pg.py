"""Postgres-live proof of the bulk importers' row reasons.

The unit suites run on SQLite, which refuses none of the rows below; Postgres
refuses each with its own SQLSTATE, and the importer reports each with fixed
wording. Every row here carries a unique value in its own cells (a device's
`field_data`, a template's `sections` defaults and description), and Postgres
refuses it in each way an import row can reach: an integer past the column's
range (asyncpg refuses it before sending, SQLSTATE 22000; a device create and a
device update), a NUL character in a text column (22021; a device name's lookup
and a template's insert), and a value longer than its column (22001; template
creates and a template update). Those reach the importer's own classification
(INV-BULK-28). A foreign key whose row another connection deleted
between the import's lookup and the insert (23503), and a device name another
connection took in the same window (23505), are answered by the create functions'
own fixed details (INV-TPL-14, INV-DEV-1), which the importer keeps. The inventory
schema has no CHECK constraint, so none is tried. Through the real routes, with the
request session on Postgres, each test asserts every row's exact outcome and reason,
that the unique value, the statement keywords, and a parameter dump are absent from
the whole response body and from every log record at INFO and above, and that the
rows before and after a refused one are judged on their own (INV-BULK-7). The first
two tests run their file as a dry run, which writes nothing, and then committed, and
the two reports are equal row for row (INV-BULK-11, INV-BULK-18).

Env contract identical to the other live suites (HERD_TEST_PG_DSN,
HERD_TEST_PG_REQUIRED). Used-ledger scoping (docs/specs/TESTING.md, issue #819):
every row this suite writes carries a random suffix in its name; a foreign template
and device are seeded, and every template and device it does not own is snapshotted
before and after each test and must be unchanged; its own rows are deleted in a
finally.
"""

from __future__ import annotations

import io
import json
import logging
import uuid

import httpx
import pytest
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.device import Device
from app.models.driver_package import DriverPackage
from app.models.template import DeviceTemplate
from app.services import bulk_service
from herd_common.pg_live_suite import pg_live_target
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PG = pg_live_target()
PG_DSN = _PG.dsn

# The unit-suite conftest sets DB_SCHEMA="", so the mapped tables carry no schema;
# the live database keeps them under "inventory".
_CONNECT_ARGS = {"server_settings": {"search_path": "inventory"}}
ADMIN = {"sub": "00000000-0000-0000-0000-00000000a002", "role": "admin", "username": "pg"}

pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")

_REFUSED = "the database refused the row"
_TYPE = f"{_REFUSED}: a value does not fit the type of its column"
_NUL = f"{_REFUSED}: a value holds a character the database cannot store, such as a NUL"
_LONG = f"{_REFUSED}: a value is longer than its column allows"


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


async def _foreign_snapshot(session_factory, tag: str) -> dict:
    async with session_factory() as s:
        devices = (await s.execute(select(Device).where(~Device.name.contains(tag)))).unique()
        templates = await s.execute(
            select(DeviceTemplate).where(~DeviceTemplate.name.contains(tag))
        )
        return {
            "devices": {
                d.id: (d.name, d.field_data, d.poll_interval_seconds, d.updated_at)
                for d in devices.scalars()
            },
            "templates": {
                t.id: (t.name, t.sections, t.description, t.updated_at) for t in templates.scalars()
            },
        }


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
        sections = [{"name": "G", "fields": [{"key": "pw", "label": "pw", "type": "password"}]}]
        template = DeviceTemplate(
            name=f"pg-live-tpl-{tag}", vendor="V", model="M", sections=sections
        )
        foreign_template = DeviceTemplate(
            name=f"pg-live-foreign-{foreign_tag}", vendor="V", model="M", sections=sections
        )
        s.add_all([template, foreign_template])
        await s.flush()
        s.add_all(
            [
                Device(
                    name=f"pg-live-dev-{tag}",
                    template_id=template.id,
                    topology_type="PHYSICAL",
                    field_data={"pw": "own"},
                ),
                Device(
                    name=f"pg-live-foreign-{foreign_tag}",
                    template_id=foreign_template.id,
                    topology_type="PHYSICAL",
                    field_data={"pw": "foreign"},
                ),
            ]
        )
        await s.commit()
    before = await _foreign_snapshot(session_factory, tag)
    assert before["devices"] and before["templates"]
    try:
        yield {
            "driver": driver.name,
            "driver_id": driver.id,
            "template": template.name,
            "device": f"pg-live-dev-{tag}",
        }
    finally:
        async with session_factory() as s:
            await s.execute(delete(Device).where(Device.name.contains(tag)))
            await s.execute(delete(DeviceTemplate).where(DeviceTemplate.name.contains(tag)))
            await s.execute(delete(DriverPackage).where(DriverPackage.name.contains(tag)))
            await s.commit()
        assert await _foreign_snapshot(session_factory, tag) == before
        async with session_factory() as s:
            await s.execute(delete(Device).where(Device.name.contains(foreign_tag)))
            await s.execute(delete(DeviceTemplate).where(DeviceTemplate.name.contains(foreign_tag)))
            await s.commit()


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


async def test_device_rows_postgres_refuses_get_fixed_reasons(
    client, session_factory, lab, tag, sentinel, caplog
):
    caplog.set_level(logging.INFO)
    base = {"template_name": lab["template"], "topology_type": "PHYSICAL"}
    fd = {"pw": sentinel}
    items = [
        {**base, "name": f"before-{tag}", "field_data": fd},
        {**base, "name": f"int-{tag}", "field_data": fd, "poll_interval_seconds": 2**40},
        {**base, "name": f"nul\x00-{tag}", "field_data": fd},
        # An update: the UPDATE's parameters carry the new field_data.
        {**base, "name": lab["device"], "field_data": fd, "poll_interval_seconds": 2**31},
        {**base, "name": f"after-{tag}", "field_data": fd},
    ]
    expected = [
        (f"before-{tag}", "create", None),
        (f"int-{tag}", "reject", _TYPE),
        (f"nul\x00-{tag}", "reject", _NUL),
        (lab["device"], "reject", _TYPE),
        (f"after-{tag}", "create", None),
    ]
    rehearsal = await _import(client, "devices", items, dry_run=True)
    assert await _names(session_factory, Device, tag) == {lab["device"]}
    committed = await _import(client, "devices", items, dry_run=False)
    for resp in (rehearsal, committed):
        _assert_clean(resp.text, sentinel)
        assert _outcomes(resp) == expected
    _assert_clean(caplog.text, sentinel)
    assert "sqlstate=22000" in caplog.text
    assert "sqlstate=22021" in caplog.text
    assert await _names(session_factory, Device, tag) == {
        lab["device"],
        f"before-{tag}",
        f"after-{tag}",
    }
    async with session_factory() as s:
        own = (await s.execute(select(Device).where(Device.name == lab["device"]))).scalar_one()
        assert own.field_data == {"pw": "own"}
        assert own.poll_interval_seconds is None


async def test_template_rows_postgres_refuses_get_fixed_reasons(
    client, session_factory, lab, tag, sentinel, caplog
):
    caplog.set_level(logging.INFO)
    sections = [
        {
            "name": "G",
            "fields": [{"key": "pw", "label": "pw", "type": "password", "default": sentinel}],
        }
    ]
    base = {"template_type": "port", "vendor": "V", "model": "M", "sections": sections}
    long_name = "L" * 256 + tag
    items = [
        {**base, "name": f"before-{tag}"},
        {**base, "name": long_name, "description": sentinel},
        {**base, "name": f"desc-{tag}", "description": f"{sentinel}\x00"},
        {**base, "name": f"vendor-{tag}", "vendor": "V" * 256, "description": sentinel},
        # An update: the UPDATE's parameters carry the new sections.
        {**base, "name": lab["template"], "template_type": "device", "part_number": "P" * 300},
        {**base, "name": f"after-{tag}"},
    ]
    expected = [
        (f"before-{tag}", "create", None),
        (long_name, "reject", _LONG),
        (f"desc-{tag}", "reject", _NUL),
        (f"vendor-{tag}", "reject", _LONG),
        (lab["template"], "reject", _LONG),
        (f"after-{tag}", "create", None),
    ]
    rehearsal = await _import(client, "templates", items, dry_run=True)
    assert await _names(session_factory, DeviceTemplate, tag) == {lab["template"]}
    committed = await _import(client, "templates", items, dry_run=False)
    for resp in (rehearsal, committed):
        _assert_clean(resp.text, sentinel)
        assert _outcomes(resp) == expected
    _assert_clean(caplog.text, sentinel)
    assert "sqlstate=22001" in caplog.text
    assert await _names(session_factory, DeviceTemplate, tag) == {
        lab["template"],
        f"before-{tag}",
        f"after-{tag}",
    }
    async with session_factory() as s:
        own = (
            await s.execute(select(DeviceTemplate).where(DeviceTemplate.name == lab["template"]))
        ).scalar_one()
        assert own.part_number is None
        assert "default" not in own.sections[0]["fields"][0]


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
