"""Unit tests for the dev-startup schema stamp helper (issues #278 and #419).

Covers the pure stamp decision and the three end-to-end paths against in-memory
SQLite with a minimal on-disk Alembic script directory: a fresh schema stamps
the head, a legacy create_all-born schema is left unstamped with a loud warning,
and an already-stamped schema is untouched. Since issue #419 "untouched" means
create_all does not run at all on a stamped schema, so a newly deployed model
table is never pre-created ahead of its own migration; the upgrade-in-place
collision test replays an unguarded bare op.create_table migration (the
execution 0014 / reservations 0013 class) against a booted stack and proves it
applies cleanly.
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


# -- Issue #419: no create_all on a migration-managed schema ------------------

SECOND_REVISION = "0002_gadget"

# Deliberately a bare, unguarded op.create_table: the same shape as execution
# migration 0014 and reservations migration 0013. The fix must make this class
# of migration safe without editing it.
_SECOND_REVISION_FILE = (
    "import sqlalchemy as sa\n"
    "from alembic import op\n"
    f'revision = "{SECOND_REVISION}"\n'
    f'down_revision = "{HEAD_REVISION}"\n'
    "branch_labels = None\n"
    "depends_on = None\n"
    "def upgrade():\n"
    "    op.create_table(\n"
    '        "gadget",\n'
    '        sa.Column("id", sa.Integer, primary_key=True),\n'
    '        sa.Column("label", sa.String),\n'
    "    )\n"
    "def downgrade():\n"
    '    op.drop_table("gadget")\n'
)

# Minimal env.py so `alembic upgrade` can run against the fixture chain; the
# stamp-only paths never load it.
_ENV_PY = (
    "from alembic import context\n"
    "from sqlalchemy import engine_from_config, pool\n"
    "config = context.config\n"
    "connectable = engine_from_config(\n"
    "    config.get_section(config.config_ini_section) or {},\n"
    '    prefix="sqlalchemy.",\n'
    "    poolclass=pool.NullPool,\n"
    ")\n"
    "with connectable.connect() as connection:\n"
    "    context.configure(connection=connection)\n"
    "    with context.begin_transaction():\n"
    "        context.run_migrations()\n"
)


def _metadata_with_gadget(metadata):
    """The deployed-new-model shape: the widget model plus a new gadget model."""
    md = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(md)
    Table("gadget", md, Column("id", Integer, primary_key=True), Column("label", String))
    return md


async def _table_names(engine):
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def test_stamped_schema_gets_no_ghost_tables(metadata, script_location, make_engine, caplog):
    """A managed schema never has create_all pre-create a newly deployed model table."""
    engine = make_engine()
    first = await create_all_and_stamp(
        engine, metadata, schema=None, script_location=script_location
    )
    assert first is SchemaInitResult.STAMPED_FRESH

    # New image boots: the model now declares "gadget", its migration not yet run.
    grown = _metadata_with_gadget(metadata)
    with caplog.at_level(logging.WARNING):
        second = await create_all_and_stamp(
            engine, grown, schema=None, script_location=script_location
        )

    assert second is SchemaInitResult.ALREADY_MANAGED
    names = await _table_names(engine)
    assert "widget" in names
    assert "gadget" not in names, "create_all must not run on a stamped schema"
    assert await _stamp_value(engine) == HEAD_REVISION


async def test_upgrade_in_place_unguarded_migration_applies_cleanly(
    metadata, script_location, tmp_path, make_engine, caplog
):
    """The issue #419 collision, end to end, against a file-backed SQLite DB.

    Old stack boots and stamps 0001; the new image ships the gadget model plus a
    bare unguarded create_table migration 0002; the new image boots BEFORE
    migrate runs; then `alembic upgrade head` replays 0002. Before the fix the
    boot pre-created gadget and 0002 crashed with "table already exists"; now
    the boot creates nothing and 0002 applies cleanly.
    """
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "upgrade_in_place.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    try:
        first = await create_all_and_stamp(
            engine, metadata, schema=None, script_location=script_location
        )
        assert first is SchemaInitResult.STAMPED_FRESH
        assert await _stamp_value(engine) == HEAD_REVISION

        # Deploy the new image: revision 0002 appears in the chain, model grows.
        (script_location / "versions" / f"{SECOND_REVISION}.py").write_text(_SECOND_REVISION_FILE)
        (script_location / "env.py").write_text(_ENV_PY)
        grown = _metadata_with_gadget(metadata)

        with caplog.at_level(logging.WARNING):
            second = await create_all_and_stamp(
                engine, grown, schema=None, script_location=script_location
            )
        assert second is SchemaInitResult.ALREADY_MANAGED
        assert "gadget" not in await _table_names(engine)
        pending = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and SECOND_REVISION in r.getMessage()
        ]
        assert pending, "expected a pending-migrations warning naming the head"
        assert HEAD_REVISION in pending[0].getMessage()
        assert "make migrate" in pending[0].getMessage()

        # The operator runs migrations: the bare create_table must not collide.
        config = Config()
        config.set_main_option("script_location", str(script_location))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.upgrade(config, "head")

        assert "gadget" in await _table_names(engine)
        assert await _stamp_value(engine) == SECOND_REVISION
    finally:
        await engine.dispose()


async def test_stamped_at_head_missing_table_warns_missing_migration(
    metadata, script_location, make_engine, caplog
):
    """Model drift with NO pending migration is loudly flagged, not silently 500ing."""
    engine = make_engine()
    await create_all_and_stamp(engine, metadata, schema=None, script_location=script_location)

    grown = _metadata_with_gadget(metadata)
    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(
            engine, grown, schema=None, script_location=script_location
        )

    assert result is SchemaInitResult.ALREADY_MANAGED
    assert "gadget" not in await _table_names(engine)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a missing-migration warning"
    message = warnings[0].getMessage()
    assert "gadget" in message
    assert "missing" in message


async def test_stamped_empty_schema_warns_stamp_mismatch(
    metadata, script_location, make_engine, caplog
):
    """A stamp with ZERO model tables gets the distinct stamp-mismatch warning.

    Reachable via a manual `alembic stamp` on an empty schema or a partial
    restore that kept alembic_version. `make migrate` cannot fix it (the stamp
    claims the missing tables' migrations already ran), so the message must
    name that remedy gap, not misdiagnose it as a missing migration.
    """
    engine = make_engine()
    # Stamp head with an EMPTY model set: alembic_version exists, no tables.
    first = await create_all_and_stamp(
        engine, MetaData(), schema=None, script_location=script_location
    )
    assert first is SchemaInitResult.STAMPED_FRESH

    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(
            engine, metadata, schema=None, script_location=script_location
        )

    assert result is SchemaInitResult.ALREADY_MANAGED
    assert "widget" not in await _table_names(engine)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a stamp-mismatch warning"
    message = warnings[0].getMessage()
    assert "does not match the actual schema contents" in message
    assert "alembic_version" in message
    # Distinct from the other managed-path warnings: neither of their remedies.
    assert "unapplied migrations" not in message
    assert "missing from the chain" not in message


async def test_stamp_ahead_of_build_head_warns_rollback(
    metadata, script_location, make_engine, caplog
):
    """A stamp not in this build's chain (image rollback) is not told to migrate.

    Nothing is absent on a rollback, so the behind-head wording ("absent until
    make migrate runs") would be false; the message must name the newer-build
    condition instead.
    """
    engine = make_engine()
    await create_all_and_stamp(engine, metadata, schema=None, script_location=script_location)

    # Simulate the rollback: the database was migrated to a revision this
    # build's chain does not contain.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = '9999_future'"))

    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(
            engine, metadata, schema=None, script_location=script_location
        )

    assert result is SchemaInitResult.ALREADY_MANAGED
    assert await _stamp_value(engine) == "9999_future"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a rollback warning"
    message = warnings[0].getMessage()
    assert "newer build" in message
    assert "9999_future" in message
    assert HEAD_REVISION in message
    assert "absent until" not in message


async def test_managed_steady_state_is_quiet(metadata, script_location, make_engine, caplog):
    """Stamped at head, no drift: no warnings on a routine restart."""
    engine = make_engine()
    await create_all_and_stamp(engine, metadata, schema=None, script_location=script_location)

    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(
            engine, metadata, schema=None, script_location=script_location
        )

    assert result is SchemaInitResult.ALREADY_MANAGED
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_managed_path_survives_broken_script_location(
    metadata, script_location, tmp_path, make_engine, caplog
):
    """The advisory drift check must never become a new boot failure mode."""
    engine = make_engine()
    await create_all_and_stamp(engine, metadata, schema=None, script_location=script_location)

    missing = tmp_path / "does-not-exist"
    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(engine, metadata, schema=None, script_location=missing)

    assert result is SchemaInitResult.ALREADY_MANAGED
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a could-not-read-chain warning"
    assert "could not read" in warnings[0].getMessage().lower()


async def test_legacy_volume_still_gets_new_tables(metadata, script_location, make_engine, caplog):
    """The unstamped limp-along path is unchanged: create_all still runs there."""
    engine = make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    grown = _metadata_with_gadget(metadata)
    with caplog.at_level(logging.WARNING):
        result = await create_all_and_stamp(
            engine, grown, schema=None, script_location=script_location
        )

    assert result is SchemaInitResult.UNSTAMPED_LEGACY
    assert "gadget" in await _table_names(engine)
    assert not await _has_alembic_version(engine)
