"""Unit tests for the issue #463 consumer schema gate.

The gate defers NATS consumer start while a migration-managed schema is
missing model tables (the boot-before-migrate window), polls the same
missing-tables check, and starts the consumer as soon as the tables exist, so
`make migrate` heals a waiting consumer without a container restart. Fresh and
legacy schema-init outcomes are never gated, the pending gate warns loudly on
a period, and shutdown cancels a still-waiting gate cleanly.
"""

import asyncio
import gc
import logging
from types import SimpleNamespace

import pytest
from herd_common import consumer_schema_gate as gate_module
from herd_common.consumer_schema_gate import (
    start_consumer_when_schema_ready,
    stop_consumer_schema_gate,
)
from herd_common.schema_init import SchemaInitOutcome, SchemaInitResult
from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.ext.asyncio import create_async_engine

GATED = SchemaInitOutcome(SchemaInitResult.ALREADY_MANAGED, frozenset({"widget"}))


@pytest.fixture
def metadata():
    md = MetaData()
    Table("widget", md, Column("id", Integer, primary_key=True), Column("name", String))
    return md


@pytest.fixture
async def engine(tmp_path):
    # File-backed, not `:memory:` (issue #534): an in-memory SQLite engine
    # uses SQLAlchemy's StaticPool, a single raw connection shared by every
    # checkout. These tests overlap a readiness-poll `engine.connect()` with
    # the test body's own `engine.begin()` (create_all), and under a slow
    # enough DB op the pool can discard one of the two concurrently-checked-
    # out raw connections without closing it. A file-backed database lets
    # SQLAlchemy's default pool for a file DSN open a real connection per
    # checkout instead of sharing the single StaticPool connection, so
    # concurrent checkouts no longer race over the same underlying
    # aiosqlite.core.Connection. See tests/test_schema_init.py's
    # `test_upgrade_in_place_unguarded_migration_applies_cleanly` for the
    # same file-backed pattern.
    db_path = tmp_path / "consumer_schema_gate.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    yield engine
    await engine.dispose()


def _app():
    return SimpleNamespace(state=SimpleNamespace())


class _Starter:
    """Stand-in for start_nats_consumer: counts calls, signals the first."""

    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()

    async def __call__(self, app):
        self.calls += 1
        self.started.set()


async def _start(app, outcome, engine, metadata, starter, **kwargs):
    await start_consumer_when_schema_ready(
        app,
        outcome,
        engine=engine,
        metadata=metadata,
        schema=None,
        start_consumer=starter,
        service="testsvc",
        **kwargs,
    )


async def test_managed_schema_with_no_drift_starts_immediately(engine, metadata):
    app = _app()
    starter = _Starter()
    outcome = SchemaInitOutcome(SchemaInitResult.ALREADY_MANAGED)

    await _start(app, outcome, engine, metadata, starter)

    assert starter.calls == 1
    assert getattr(app.state, "consumer_schema_gate_task", None) is None


async def test_fresh_and_legacy_outcomes_never_gate(engine, metadata):
    """Only the managed path can gate, even if a missing set were reported."""
    for action in (SchemaInitResult.STAMPED_FRESH, SchemaInitResult.UNSTAMPED_LEGACY):
        app = _app()
        starter = _Starter()
        outcome = SchemaInitOutcome(action, frozenset({"widget"}))
        assert outcome.consumer_should_wait is False

        await _start(app, outcome, engine, metadata, starter)

        assert starter.calls == 1
        assert getattr(app.state, "consumer_schema_gate_task", None) is None


async def test_gated_defers_then_starts_when_table_appears(engine, metadata, caplog):
    """The heal path: consumer waits, `make migrate` creates the table, consumer starts."""
    app = _app()
    starter = _Starter()

    with caplog.at_level(logging.INFO):
        await _start(app, GATED, engine, metadata, starter, poll_interval_seconds=0.02)

        assert starter.calls == 0, "gated start must not start the consumer"
        task = app.state.consumer_schema_gate_task
        assert task is not None and not task.done()
        deferral = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert deferral, "expected a loud deferral warning"
        assert "widget" in deferral[0].getMessage()
        assert "make migrate" in deferral[0].getMessage()

        # A few polls against the still-missing table must not start anything.
        await asyncio.sleep(0.06)
        assert starter.calls == 0

        # Stand in for `make migrate` creating the table.
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        await asyncio.wait_for(starter.started.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)

    assert starter.calls == 1
    ready = [r for r in caplog.records if "schema is ready" in r.getMessage()]
    assert ready, "expected a consumer-started log line"


async def test_gated_warns_periodically_while_waiting(engine, metadata, caplog):
    app = _app()
    starter = _Starter()

    with caplog.at_level(logging.WARNING):
        await _start(
            app,
            GATED,
            engine,
            metadata,
            starter,
            poll_interval_seconds=0.02,
            warn_interval_seconds=0.0,
        )

        # Poll for the warning instead of sleeping a fixed budget (issue
        # #534 follow-up): a fixed sleep(0.1) is exactly five 0.02s poll
        # periods, so it only has margin for a "still waiting" warning to
        # land if every poll's readiness query is fast. Under real DB
        # latency (slow disk, CI's coverage load, or the shielded
        # cancellation wait added for #534, which makes shutdown wait for
        # an in-flight query to finish rather than abandoning it) a single
        # poll can outlast that whole budget, and the fixed sleep would
        # then stop the gate before it ever logs, flaking this assertion
        # for a reason that has nothing to do with what the test checks.
        # Polling up to a generous ceiling waits only as long as actually
        # needed and still fails deterministically if the warning never
        # comes.
        deadline = asyncio.get_running_loop().time() + 5.0
        while not any("still waiting" in r.getMessage() for r in caplog.records):
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)

        await stop_consumer_schema_gate(app)

    assert starter.calls == 0
    waiting = [r for r in caplog.records if "still waiting" in r.getMessage()]
    assert waiting, "expected periodic still-waiting warnings"
    assert "widget" in waiting[0].getMessage()


async def test_stop_cancels_pending_gate(engine, metadata):
    app = _app()
    starter = _Starter()

    await _start(app, GATED, engine, metadata, starter, poll_interval_seconds=30.0)
    task = app.state.consumer_schema_gate_task
    assert not task.done()

    await stop_consumer_schema_gate(app)
    assert task.cancelled()
    assert starter.calls == 0

    # Idempotent: a second stop finds the cancelled task and is a no-op.
    await stop_consumer_schema_gate(app)


async def test_stop_is_noop_when_never_gated():
    # Must not raise.
    await stop_consumer_schema_gate(_app())


async def test_readiness_check_failure_is_retried(engine, metadata, caplog, monkeypatch):
    """A transient DB error in the poll is logged and retried, never fatal."""
    real_check = gate_module.missing_model_tables
    failures = {"remaining": 2}

    async def _flaky(*args, **kwargs):
        if failures["remaining"] > 0:
            failures["remaining"] -= 1
            raise RuntimeError("transient database trouble")
        return await real_check(*args, **kwargs)

    monkeypatch.setattr(gate_module, "missing_model_tables", _flaky)

    app = _app()
    starter = _Starter()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    with caplog.at_level(logging.WARNING):
        await _start(app, GATED, engine, metadata, starter, poll_interval_seconds=0.02)
        await asyncio.wait_for(starter.started.wait(), timeout=2.0)
        await asyncio.wait_for(app.state.consumer_schema_gate_task, timeout=2.0)

    assert starter.calls == 1
    retried = [r for r in caplog.records if "could not check readiness" in r.getMessage()]
    assert len(retried) == 2


async def test_cancel_mid_query_does_not_leak_the_connection(engine, metadata, monkeypatch):
    """Regression for issue #534: a cancel mid-query must not leak the connection.

    Patches `aiosqlite.core.Connection._execute` (scoped via monkeypatch) to
    signal an asyncio.Event and yield before running the real op, pinning
    the moment a query is genuinely in flight so the cancel below lands
    there deterministically, with no real-time sleep budget needed.

    Relies on the project's `filterwarnings = ["error"]` rather than a
    manual `catch_warnings`: `Connection.__del__` warns through pytest's
    unraisable-exception hook, not the normal `warnings` call path, so
    `catch_warnings` around `gc.collect()` would not observe it, while
    `filterwarnings = ["error"]` is the same mechanism that produced the
    original failure, so a reintroduced leak fails this test directly.
    """
    import aiosqlite.core as aiosqlite_core

    query_in_flight = asyncio.Event()
    real_execute = aiosqlite_core.Connection._execute

    async def _slow_execute(self, fn, *args, **kwargs):
        query_in_flight.set()
        # Yield back to the event loop so stop_consumer_schema_gate's cancel
        # has a chance to be delivered while this op is still in flight on
        # the worker thread, exactly as in the original leak.
        await asyncio.sleep(0.05)
        return await real_execute(self, fn, *args, **kwargs)

    monkeypatch.setattr(aiosqlite_core.Connection, "_execute", _slow_execute)

    app = _app()
    starter = _Starter()

    await _start(app, GATED, engine, metadata, starter, poll_interval_seconds=0.001)
    await asyncio.wait_for(query_in_flight.wait(), timeout=2.0)

    # Cancel immediately, while the readiness query is suspended mid-flight.
    await stop_consumer_schema_gate(app)

    # No connection may still be open past this point: dispose the engine,
    # then force collection so a leaked connection's __del__ fires here, in
    # this test, under the project's filterwarnings = ["error"], rather than
    # at an arbitrary later point.
    await engine.dispose()
    gc.collect()
