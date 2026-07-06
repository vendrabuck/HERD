"""Unit tests for the dev-startup schema stamp helper (issue #278).

Covers the pure stamp decision and the three end-to-end paths against in-memory
SQLite with a minimal on-disk Alembic script directory: a fresh schema stamps
the head, a legacy create_all-born schema is left unstamped with a loud warning,
and an already-stamped schema is untouched.
"""

import logging

import pytest
from herd_common.schema_init import (
    SchemaInitResult,
    create_all_and_stamp,
    decide_schema_action,
)
from sqlalchemy import Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import create_async_engine

HEAD_REVISION = "0001_head"

_REVISION_FILE = (
    f'revision = "{HEAD_REVISION}"\n'
    "down_revision = None\n"
    "branch_labels = None\n"
    "depends_on = None\n"
    "def upgrade():\n    pass\n"
    "def downgrade():\n    pass\n"
)


@pytest.fixture
def script_location(tmp_path):
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions.parent / "versions" / "0001_head.py").write_text(_REVISION_FILE)
    return tmp_path / "migrations"


@pytest.fixture
def metadata():
    md = MetaData()
    Table("widget", md, Column("id", Integer, primary_key=True), Column("name", String))
    return md


@pytest.fixture
async def make_engine():
    engines = []

    def _factory():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        engines.append(engine)
        return engine

    yield _factory
    for engine in engines:
        await engine.dispose()


async def _stamp_value(engine):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()


async def _has_alembic_version(engine):
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    return "alembic_version" in names


# -- Pure decision logic ------------------------------------------------------


def test_decide_fresh_stamps():
    assert decide_schema_action(had_tables=False, had_stamp=False) == SchemaInitResult.STAMPED_FRESH


def test_decide_legacy_unstamped():
    assert (
        decide_schema_action(had_tables=True, had_stamp=False) == SchemaInitResult.UNSTAMPED_LEGACY
    )


def test_decide_already_managed_when_stamped():
    assert decide_schema_action(had_tables=True, had_stamp=True) == SchemaInitResult.ALREADY_MANAGED
    # A stamp always wins, even without observed tables.
    assert (
        decide_schema_action(had_tables=False, had_stamp=True) == SchemaInitResult.ALREADY_MANAGED
    )


# -- End-to-end behavior ------------------------------------------------------


async def test_fresh_schema_stamps_head(metadata, script_location, make_engine, caplog):
    engine = make_engine()
    with caplog.at_level(logging.INFO):
        result = await create_all_and_stamp(
            engine, metadata, schema=None, script_location=script_location
        )
    assert result is SchemaInitResult.STAMPED_FRESH
    assert await _stamp_value(engine) == HEAD_REVISION
    assert any(HEAD_REVISION in r.getMessage() for r in caplog.records)


async def test_legacy_unstamped_tables_warn_and_do_not_stamp(
    metadata, script_location, make_engine, caplog
):
    engine = make_engine()
    # Simulate a create_all-born volume: tables exist, no stamp.
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(
            engine, metadata, schema=None, script_location=script_location
        )

    assert result is SchemaInitResult.UNSTAMPED_LEGACY
    assert not await _has_alembic_version(engine)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a loud legacy-volume warning"
    assert "Recreate the Postgres volume" in warnings[0].getMessage()


async def test_already_stamped_schema_is_untouched(metadata, script_location, make_engine):
    engine = make_engine()
    # First call stamps a fresh schema.
    first = await create_all_and_stamp(
        engine, metadata, schema=None, script_location=script_location
    )
    assert first is SchemaInitResult.STAMPED_FRESH

    # A second call must not re-stamp or alter the recorded revision.
    second = await create_all_and_stamp(
        engine, metadata, schema=None, script_location=script_location
    )
    assert second is SchemaInitResult.ALREADY_MANAGED
    assert await _stamp_value(engine) == HEAD_REVISION


async def test_fresh_schema_without_revisions_is_left_unstamped(
    metadata, tmp_path, make_engine, caplog
):
    empty = tmp_path / "migrations"
    (empty / "versions").mkdir(parents=True)
    engine = make_engine()
    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(engine, metadata, schema=None, script_location=empty)
    assert result is SchemaInitResult.STAMPED_FRESH
    assert not await _has_alembic_version(engine)
