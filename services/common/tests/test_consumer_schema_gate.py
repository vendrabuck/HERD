"""Unit tests for the issue #463 consumer schema gate.

The gate defers NATS consumer start while a migration-managed schema is
missing model tables (the boot-before-migrate window), polls the same
missing-tables check, and starts the consumer as soon as the tables exist, so
`make migrate` heals a waiting consumer without a container restart. Fresh and
legacy schema-init outcomes are never gated, the pending gate warns loudly on
a period, and shutdown cancels a still-waiting gate cleanly.
"""

import asyncio
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
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
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
        await asyncio.sleep(0.1)
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
